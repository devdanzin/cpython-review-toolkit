# CPython false-positive taxonomy (cpython-review-toolkit)

The precision-decay guard for the informed-explore loop. Each entry is a pattern
the scanners *can* surface but that is usually **not** a bug in CPython's own
code. During triage, skip these classes — or explicitly justify why this instance
is different. Add to this file whenever a review confirms a new FP class.

---

## PyErr_Clear / exception state

- **`PyErr_Clear()` after a sentinel-returning lookup is idiomatic.** After
  `PyObject_GetAttr` / `PyDict_GetItemWithError` / `PyMapping_GetOptionalItem`
  where a missing key is expected, clearing an `AttributeError`/`KeyError` is
  correct — *outside* the destructor family. The `pyerr-clear-auditor` is scoped
  to dealloc/clear/finalize precisely to avoid this; a hit there is real.
- **A destructor that already saves/restores** (`PyErr_GetRaisedException` /
  `PyErr_Fetch` / `PyErr_WriteUnraisable`) is fine — but verify the save/restore
  actually brackets the flagged clear in a large function (whole-function
  suppression can hide a second, unguarded clear).

## Recursion guards

- **Guarded by the dispatcher — but NOT for hash.** A leaf slot reached only
  through `PyObject_Repr` (`Objects/object.c:759`), `PyObject_Str` (`:800`) or
  `PyObject_RichCompare` (`:1099`) is safe *if it is never reached directly*,
  because those three do wrap `_Py_EnterRecursiveCallTstate`. Confirm the call
  graph before dismissing.

  **`PyObject_Hash` (`Objects/object.c:1158`) has NO recursion guard.** Verified
  against main @ 3.16.0a0. A `tp_hash` slot that descends into element hashes is
  therefore unguarded at *every* level and overflows the native C stack (SIGSEGV,
  not a catchable `RecursionError`). Never dismiss a hash-descent finding as
  "dispatcher-guarded": that asymmetry between the four dispatchers is exactly
  what makes `tuple_hash` (gh-154318 / CPY-0001), `union_hash`, `ga_hash` and
  `frozendict_pair_hash` real bugs. This entry previously listed `PyObject_Hash`
  among the guarded dispatchers, which was factually wrong and would have
  suppressed the entire true-positive class.
- **Non-nestable receiver.** A `*_hash`/`*_repr` on a type whose elements can
  never be the same or another container (e.g. a code object's fixed fields) can
  descend without a guard because the depth is bounded. ACCEPTABLE — but state
  the bound.
- **Iterative deallocation bounds a `tp_dealloc` — but do NOT look for
  `Py_TRASHCAN_BEGIN` to decide that.** On main (verified @ 3.16.0a0) the old
  macros are **empty backwards-compat shims** — `Include/cpython/object.h:446-447`,
  literally `#define Py_TRASHCAN_BEGIN(op, dealloc)` with an empty body — and
  **zero call sites remain in `Objects/` or `Modules/`**. The live mechanism is
  automatic inside `_Py_Dealloc`, via `_PyTrash_thread_deposit_object()` /
  `_PyTrash_thread_destroy_chain()`.

  So the correct test is *not* "is this dealloc trashcan-protected?" (nothing is,
  by that marker — an agent applying the old test finds no marker anywhere and
  wrongly promotes every dealloc finding). The test is **what the descent goes
  through**: a `tp_dealloc` that recurses only by `Py_DECREF`-ing contained
  objects is bounded by the automatic chain and is a FP; a dealloc that recurses
  some *other* way is not covered and can still overflow the C stack — see
  `gh-149146 tuple_dealloc` (recursion during MemoryError unwind), which is a
  real catalogued bug.

## Uninitialized dealloc

- **Zeroing allocator.** `PyType_GenericAlloc` / `_PyType_AllocNoTrack` /
  `*_GC_Calloc` zero the object; a following early free is safe. The scanner
  excludes these, but a wrapper macro may hide one.
- **`type->tp_alloc(type, n)` is NOT unconditionally zeroing** — *amended
  2026-07-25*. An earlier draft of this entry listed `tp_alloc` alongside
  `PyType_GenericAlloc` as if the slot always zeroed. It does not: a type may
  install its own `allocfunc`. `Modules/_datetimemodule.c` installs two —
  `time_alloc` (`:879`, wired positionally at `:5382`) and `datetime_alloc`
  (`:891`, at `:7349`) — both `PyObject_Malloc` + `_PyObject_Init` with no
  `memset`, and the file's own comment (`:861-862`) says so: *"All data members
  remain uninitialized trash."* `time_dealloc`/`datetime_dealloc` then `switch`
  on the scalar `hastzinfo` to decide whether to `Py_XDECREF(self->tzinfo)` —
  the blake2 `impl` shape (gh-152851) exactly. **There is no live bug there
  today**: all nine `tp_alloc` call sites in that file set `hastzinfo` in the
  statement immediately after the allocation. But *resolve the slot* before
  dismissing on this ground; do not dismiss on the spelling. Tree-wide at
  3.16.0a0 these are the only two non-zeroing `tp_alloc`s (`bytes_alloc`,
  `_PyType_AllocNoTrack` and `PyType_GenericAlloc` all zero), and
  `scan_uninit_dealloc.py` now detects them mechanically
  (`_nonzeroing_tp_allocs`).
- **`tp_dealloc` guards each member with `Py_XDECREF`** *and* the members were
  NULL-initialized before the failing step — Py_XDECREF(NULL) is a no-op, so no
  crash. Only a member left as *garbage* (not NULL) at the free point is a bug.

## NULL checks / error paths

- **Infallible-by-construction returns.** `Py_None`/`Py_True`/`Py_False`, interned
  singletons, and `_Py_ID(...)` never return NULL; a missing check is not a bug.
- **Checked via a macro the scanner doesn't model.** `Py_SETREF` / `Py_XSETREF`
  and `Py_CLEAR` handle NULL internally; assignments through them are safe.
  `Py_XDECREF(NULL)` is likewise a documented no-op: a `Py_XDECREF` on a path
  where the pointer is provably NULL is dead code, not a crash.
- **Result is returned directly — NULL propagation *is* the error handling.**
  The dominant FP class in `Objects/`: 9 of 21 candidates in the sample run,
  ~43%. `res = PyUnicode_FromFormat(...); return res;` is the canonical `tp_repr`
  body and is correct — the caller sees NULL and an exception is already set.
  Only report an unchecked value that is *dereferenced*. Exemplars:
  `Objects/cellobject.c:124` `cell_repr`, `Objects/descrobject.c:615`
  `calculate_qualname`, `Objects/weakrefobject.c:226`/`:231` `weakref_repr`,
  `Objects/weakrefobject.c:785` `proxy_iternext` (`PyIter_Next` → NULL is the
  StopIteration protocol, not an error).
- **The NULL check is the loop condition.** `while ((pair = PyIter_Next(it)) != NULL)`
  and `for (key = PyIter_Next(i); key; key = PyIter_Next(i))` test the value in
  the controlling expression, on the same line as the assignment. Exemplars:
  `Objects/odictobject.c:2234`, `:2314`, `Objects/dictobject.c:4346` `dict_merge`.
- **The check is on the struct-field lvalue.** `ub->args = PyList_New(0)` is
  checked as `if (ub->args == NULL)`; a scanner that captured only the trailing
  identifier (`args`) looks for the wrong name. Exemplars:
  `Objects/unionobject.c:145`, `:174`.
- **The check is on an aliased lvalue.** `args = tuple_args = PySequence_Tuple(args);`
  is checked two lines later as `if (args == NULL)` — the *outer* target, not the
  innermost one. Exemplars: `Objects/genericaliasobject.c:192`, `:460`.
- **Correct by construction / interprocedural.** The callee is NULL-tolerant
  (`Objects/genericaliasobject.c:647` passes an unchecked `obj` to `set_orig_class`,
  whose first statement is `if (obj != NULL)`), or the check *is* the return
  expression (`Objects/tupleobject.c:1068`: `*pv = PyTuple_New(newsize);
  return *pv == NULL ? -1 : 0;`). Out of reach for single-function analysis;
  establish the callee's contract before promoting.
- **Out-parameter fills are checked through the pointer, or by the caller.**
  `*result = PyObject_GetItem(obj, key); if (*result) return 1;`
  (`Objects/abstract.c:215` `PyMapping_GetOptionalItem`) is checked; so is
  `*myerrno`/`if (!*myerrno)` (`Objects/exceptions.c:2099`). Where there is no
  local check at all (`Objects/unicode_format.c:716`), the obligation belongs to
  the caller — verify there before reporting.
- **`sizeof *x` inside the allocation's own argument list is not a use of the
  result.** `struct unpacker *x = PyMem_Malloc(sizeof *x);` is the standard
  CPython idiom. Ten of the 37 `Modules/` candidates in the calibration run were
  this. The same applies to `x = PyMem_Malloc(n * sizeof(x[0]))`.

## Refcounts

- **Borrowed ref under a known-live owner.** A borrowed item is safe across a call
  if a strong reference is provably held elsewhere for the duration (e.g. the
  container is a local the callee cannot reach). Establish the owner before
  dismissing a borrowed-across-call finding.
- **Stolen-ref APIs used correctly.** `PyList_SET_ITEM`/`PyTuple_SET_ITEM` on a
  freshly-created, not-yet-published container are the normal fast path.

## Free-threading (when the FT detectors land)

- **Immortal objects** (`_Py_IMMORTAL_REFCNT`) are not raced by refcount ops.
- **Access under `Py_BEGIN_CRITICAL_SECTION`** for the relevant object is
  protected; a plain read is only a race if a *concurrent* writer exists without
  the same critical section.

## Error paths — `unchecked_return` FP classes (scan_error_paths.py, 2026-07)

Measured on a 14-file `Objects/` sample: **28 of 28** `unchecked_return`
candidates were false positives, in five mechanical classes. The scanner now
suppresses all of them; they are recorded here because an agent reading code by
hand meets the same shapes.

- **Value returned directly (46% of that sample's noise).**
  `res = PyUnicode_FromFormat(...); ...; return res;` needs no NULL check — the
  callee's exception propagates untouched. This also covers the wrapped form,
  `return set_orig_class(obj, self);` (`Objects/genericaliasobject.c:647`).
- **Positive-form and loop-condition checks.** `if (v)`, `if (*v)`,
  `if (v != NULL)`, `while ((v = PyIter_Next(it)) != NULL)`,
  `for (k = PyIter_Next(it); k; k = PyIter_Next(it))` and
  `return v == NULL ? -1 : 0` are all checks. Only `== NULL`, `!v` and `== 0`
  used to count as one.
- **Aliased assignment.** `a = b = API(...)` — the check may be written against
  either name (`args = tuple_args = PySequence_Tuple(args); if (args == NULL)`).
- **Struct-member destination.** `ub->args = PyList_New(0)` is checked as
  `if (ub->args == NULL)`; a bare-identifier LHS capture misses it.
- **Out-parameter store.** `*result = PyObject_GetItem(obj, key);` and
  `*method = PyObject_GetAttr(obj, name);` hand the NULL check to the caller by
  contract (`Objects/abstract.c:215`, `Objects/object.c:1670`).
- **NULL-tolerant consumer.** `PyModule_Add` / `PyModule_AddObject` /
  `PyModule_AddObjectRef` reject NULL explicitly and propagate the pending
  exception (`Python/modsupport.c:602`); `Py_XDECREF`, `Py_CLEAR` and
  `Py_XSETREF` are NULL-safe. Passing an unchecked result to one of these is the
  house idiom, not a bug.

## Allocators — who owes the `MemoryError`

- **Allocators that raise for you.** `PyObject_New` / `PyObject_NewVar` /
  `PyObject_GC_New` / `PyObject_GC_NewVar` / `PyType_GenericAlloc` / `tp_alloc`
  (which raises even in the hand-written non-zeroing form — `time_alloc`
  returns `PyErr_NoMemory()`; *raising* and *zeroing* are separate questions,
  see the Uninitialized-dealloc section),
  and the object constructors (`PyList_New`, `PyTuple_New`, `PyDict_New`, ...)
  set `MemoryError` themselves. A failure branch that just returns the sentinel
  after one of these is correct.
- **Allocators that do not.** Only the raw family —
  `PyMem_Malloc`/`Calloc`/`Realloc`, `PyMem_RawMalloc`/`RawCalloc`/`RawRealloc`,
  `PyObject_Malloc`/`Calloc`/`Realloc`, and plain `malloc`/`calloc`/`realloc` —
  needs an explicit `PyErr_NoMemory()`. **`PyMem_New` and `PyMem_Resize` belong
  to this group**: they are plain macros over `PyMem_Malloc` / `PyMem_Realloc`
  (`Include/pymem.h:63,73`) and do *not* raise. An earlier draft of this
  taxonomy listed `PyMem_New` as exempt; that was wrong.
- **Obligation deferred to the caller.** A thin static allocation helper
  (`list_allocate_array`, `new_values` in `Objects/`) may return NULL and let
  every caller raise. Check the call sites before reporting the helper.
- **The raw memory layer cannot raise.** `Objects/obmalloc.c` (`_PyMem_Strdup`,
  `arena_map_get`, `new_arena`, `_PyMem_init_obmalloc`, ...) runs where no
  thread state need exist; returning NULL without an exception is the contract
  there, not a bug. Six of the ten `alloc_null_no_memerror` candidates in
  `Objects/` are this class.

## PyErr_Clear — widened-scanner FP classes

Added after `scan_pyerr_clear` was widened past the destructor family; measured
on main @ 3.16.0a0 over `Objects/` + `Modules/` + `Python/`.

The "sentinel-returning lookup" entry above is correct and load-bearing — it
predicted the split almost exactly: **47 of the 86 attributed clears in
`Objects/` are `PyErr_ExceptionMatches`-narrowed** and must stay suppressed. Two
refinements and five new classes came out of measuring the widened rules.

- **Refinement: an *unfiltered* clear after a call that runs arbitrary Python is
  NOT in the idiomatic class.** `PyObject_Hash`, `PyObject_GetBuffer`,
  `PyNumber_AsSsize_t`, `PyObject_Call*`, an import hook, or a slot dispatched
  off a runtime object (`pb->bf_getbuffer`) can raise *anything*. Clearing
  without a narrowing test discards `MemoryError` / `KeyboardInterrupt` /
  `RecursionError` along with the expected `TypeError`. Require an
  `ExceptionMatches` narrowing before calling one of these acceptable.
  (`Objects/unionobject.c:172` is the archetype; `set_orig_class` in
  `Objects/genericaliasobject.c` is the guarded twin.)
- **Refinement: whole-function save/restore suppression is unsound**, as the
  entry above warns. Measured: it also hides a *second* clear even when the pair
  brackets the first — `xibufferview_dealloc` (`Modules/_interpretersmodule.c`)
  has clears at `:175` and `:183` and only one was ever reported, because the
  shared `deduplicate_findings` collapses same-file same-type findings by a
  *normalized* detail string that erases function names and line numbers.
  Distinct sites in one file must not be deduplicated that way.

New FP classes, each one measured as a real false positive of a widened rule:

- **Early-return guard clause.** CPython narrows by guard clause far more often
  than by nesting: `if (!PyErr_ExceptionMatches(PyExc_KeyError)) return -1;` then
  `PyErr_Clear();`. The clear has *no enclosing conditional* but is fully
  dominated by the negation of the guard. Any success-path rule must treat
  preceding terminating `if`s as dominating conditions, in both polarities —
  `if (key != NULL) return key;` before a clear equally proves the call failed.
  (`Objects/abstract.c:223`, `Objects/typeobject.c:9748`,
  `Objects/moduleobject.c:1354/1390`, `Objects/memoryobject.c:3006`.)
- **Macro-hidden return.** `Py_RETURN_TRUE` / `Py_RETURN_NONE` /
  `Py_RETURN_NOTIMPLEMENTED` / `Py_UNREACHABLE()` / a module-local `FAIL(...)`
  parse as ordinary expression statements, so a guard clause ending in one does
  not *look* terminating to tree-sitter. Treat them as terminators.
  (`Modules/_interpretersmodule.c:1346`, `Modules/_testcapimodule.c:766`.)
- **Non-identifier lvalue.** The tested value is often a struct member or a
  dereferenced out-parameter, not a local: `interp->dict = PyDict_New();
  if (interp->dict == NULL)`, `*pmod = parse(...); if (*pmod == NULL)`. Matching
  assignment targets by bare identifier makes these read as success paths.
  (`Python/pystate.c:1280`, `:2125`, `Python/pythonrun.c:302`.)
- **File-local status helper.** `if (random_seed_urandom(self) < 0)` is an error
  test even though the callee is not `Py`-prefixed. Recognize *any* call whose
  result is compared against `NULL`/`0`/`-1` as an error test — but do **not**
  extend that to a bare predicate call with no comparison
  (`if (!equiv_shape(vv, ww))` sets nothing and is a true positive).
  (`Modules/_randommodule.c:305`, `Modules/_remote_debugging/frame_cache.c:211`.)
- **Wrong-polarity branch attribution.** A clear inside `if (module) { ... }` is
  in the branch taken when the import *succeeded*; it is not reacting to that
  failure. Attributing it to the enclosing call's failure is an FP. Only the
  innermost enclosing branch counts, and only on its failure side.
  (`Modules/_testcapimodule.c:815`.)
- **Sibling-branch exception consumer.** `if (tracebacks_enabled)
  PyErr_FormatUnraisable(...); else PyErr_Clear();` — the function exists to
  consume the pending exception one way or the other. A report/chain API in the
  clear's own `if`/`else` means the clear is deliberate.
  (`Modules/_sqlite/connection.c:929`.)
- **Statically-known type slot.** `PyUnicode_Type.tp_hash(key)` is a fixed C
  function; no user code runs. Only a slot read off a *runtime* object
  (`pb->bf_getbuffer`, `Py_TYPE(x)->tp_descr_get`) is arbitrary Python.
  (`Objects/dictobject.c:1336`.)
- **The API's own implementation.** `Python/errors.c:545 PyErr_Clear()` is the
  public wrapper around `_PyErr_Clear(tstate)`. Scanners that match the private
  alias must not flag the definition.
- **Diagnostic and test-support code is dense with deliberate swallows.**
  `Modules/_testcapimodule.c`, `Modules/_xxtestfuzz/`, `Python/traceback.c`,
  `Python/pythonrun.c`'s interactive-prompt helpers and `Python/errors.c`'s
  unraisable writers account for most of the tree-wide hits of the widened
  rules. They are genuine instances of the pattern and almost always POLICY or
  ACCEPTABLE. `Modules/_testcapimodule.c return_null_without_error` clears
  *precisely so* `_Py_CheckFunctionResult` can detect the resulting bug.

## Uninitialized dealloc (v0.8 additions)

- **Wrapper constructor that NULL-inits every slot.** An allocation routed
  through a project-local helper that zeroes all members —
  `PyStructSequence_New` (`Objects/structseq.c:65`, all `n_fields` slots NULLed
  before anything fallible), `PyTuple_New`, `PyList_New(0)` — is safe, and the
  scanner never sees the raw allocator at the call site. `Objects/structseq.c`
  is a **silent correct negative** for this shape, not an unexamined file. Do
  not re-hunt it.
- **Shared `fail:` label.** A `Py_XDECREF(var)` on an error label reachable only
  *before* the allocation (var still NULL) or *after* every member is written is
  not a finding — there is no member write after the free.
  `Objects/typeobject.c:11343 slot_bf_getbuffer` is the exemplar.
- **Non-NULL sentinel initializer.** `new->ob_exports = 0;`
  (`Objects/bytearrayobject.c:164`) initializes the member just as effectively
  as `= NULL`; a gate that only matches `= NULL` mis-reads it as uninitialized.
- **Scalar member left unset.** A `Py_ssize_t` / `int` / enum member written
  after the free is only a bug if the destructor *acts on it* — decrefs it,
  switches on it, or uses it as a bound over an array it decrefs. Members the
  destructor never reads (`_sre.c:2955` `pos`/`endpos`/`lastindex`,
  `_decimal.c:1443` `tstate`/`modstate`, `_ssl.c:942` `socket_type`) are FPs.
  The converse is *not* an FP: blake2's `impl` enum (gh-152851) and
  `elementiter_dealloc`'s `parent_stack_used` loop bound are scalars that do
  drive teardown.
- **Plain `#ifdef` block with no `#else`.** Only one arm of a preprocessor
  conditional is ever compiled, so `co->_co_unique_id = …` inside
  `#ifdef Py_GIL_DISABLED … #endif` *does* dominate a later `Py_DECREF(co)`
  outside the block (`Objects/codeobject.c:736`). Dominance is broken only by a
  *different arm* of the same group.
- **A clean OOM sweep is not an exoneration.** gh-151815 (`template_iter`)
  survives 60/60 clean `MemoryError` runs and is still live at 3.16.0a0: the
  shape crashes only on a *dirty* recycled block, and `templateiter_clear` NULLs
  both members before `tp_free`, so a same-type block always returns clean.
  Record such results as "unstable trigger", never as "fixed".

## Memory patterns (v0.8 additions)

- **`bounded-by-an-existing-allocation`.** `Py_SIZE(x)`, `PyTuple_GET_SIZE(x)`,
  `PyList_GET_SIZE`, `PyBytes_GET_SIZE`, `PyByteArray_GET_SIZE`,
  `PyUnicode_GET_LENGTH` and the other concrete-type accessors return the length
  of an object that is *already in memory*, so `n * sizeof(ptr)` cannot overflow
  `Py_ssize_t` — the container itself would have had to exceed
  `PY_SSIZE_T_MAX / elemsize` bytes. `Objects/call.c:491` and
  `Objects/listobject.c:2985` are the exemplars; this was 100% of
  `alloc_size_overflow`'s noise on `Objects/`. **Not** in this class:
  `PyLong_As*` results, `PyNumber_AsSsize_t`, `PyArg_Parse*` outputs, the
  protocol-dispatched `PyObject_Length` / `PySequence_Size` /
  `PyObject_LengthHint` (a Python `__len__` may return any `Py_ssize_t` with no
  memory behind it), and anything read out of a mutable type dictionary.
- **Narrow-typed `nitems`.** On LP64 an `int` / `short` / `char` count cannot
  make `nitems * tp_itemsize` wrap a 64-bit `size_t`, so
  `PyObject_GC_NewVar(..., slots)` with `int slots` is safe
  (`Objects/frameobject.c:2119`, `genobject.c:1100`, `genobject.c:1170`,
  `memoryobject.c:649`).
- **A `< 0` sign check is not an overflow guard.** `PyStructSequence_New` has
  `if (size < 0) return NULL;` and still hands `2**62` to `_PyObject_VAR_SIZE`.
  Only a `PY_SSIZE_T_MAX / elemsize` division check or
  `__builtin_mul_overflow` counts.
- **`PyObject_GC_UnTrack` (the function) is untracked-tolerant, not NULL-safe.**
  It re-checks `_PyObject_GC_IS_TRACKED`, which dereferences its argument
  unconditionally. A `gc_untrack_without_track` candidate whose *own type's*
  `tp_dealloc` uses the function form is an ACCEPTABLE true negative
  (`templateobject.c:232`, `interpolationobject.c:218`, `codeobject.c:751`,
  `listobject.c:262`, `context.c:895`) — but do not restate the reason as
  "NULL-safe".
- **A sibling type's macro is not this type's macro.** A file-level
  `_PyObject_GC_UNTRACK` test lets a safe constructor through whenever any other
  type in the same file uses the macro; `PyList_New` was reported only because
  `listiter_dealloc` uses it while `list_dealloc` does not. Resolve the type.

## Refcounts — borrowed-ref-across-call (added from the `scan_refcounts` rebuild)

Every entry below was a *measured* false positive of the borrowed-ref rules on
CPython main @ 3.16.0a0, and each one is now gated in the scanner. They are
listed here because the same shapes will fool a human reading code.

- **Mutually exclusive preprocessor branches.** A `Py_XDECREF(v)` before an
  `#else` and a use of `v` after it never run in the same build.
  `Objects/dictobject.c`'s `Py_GIL_DISABLED` lookups are the canonical case:
  the free-threaded branch DECREFs the out-parameter, the default branch is the
  one that assigns it. Never reason across a `#if`/`#else`/`#endif` boundary.
- **Out-parameter re-binding.** `Py_XDECREF(file); PySys_GetOptionalAttr(&_Py_ID(stderr), &file)`
  releases the *previous* value; `&file` overwrites it. Same for `_PyErr_Fetch(tstate, &exc_type, ...)`.
  A `&var` handed to a call is an assignment.
- **Shadowed re-declaration.** `Py_XDECREF(loader); if (!has_loader) { PyObject *loader = ...; }`
  — the inner `loader` is a different variable (`Python/pylifecycle.c` `add_main_module`).
- **Macro-hidden assignment.** `Py_CLEAR(obj); ... ASSIGN_PTR(obj, PyObject_CallMethod(...))`
  re-binds `obj` inside a SCREAMING_CASE macro (`Modules/_decimal`).
- **`Py_CLEAR` NULLs its own operand.** A later read of that same variable is a
  NULL read, not a dangling one. Only an *alias* is still exposed.
- **A struct member is not a local.** `self->last` must not read as a use of a
  local named `last` (`Modules/_elementtree.c` `treebuilder_handle_end`).
- **The reference was published before it was dropped.** `PyModule_AddType(m, T); Py_DECREF(T);`
  then `T->tp_dict` — the module holds it. Generalises the existing
  "borrowed under a known-live owner" entry to registration APIs.
- **The INCREF is written against the source, not the destination.**
  `Py_INCREF(lz->lz_attr); fromlist = lz->lz_attr;` makes `fromlist` an owner
  even though no `Py_INCREF(fromlist)` appears (`Python/import.c`).
- **Owner swap, not stale drop.** `old = self->f; self->f = new; Py_XDECREF(old);`
  is correct: once the slot is overwritten the local is the sole owner of the
  old value (`defaultdict.__init__`). The *dangerous* variant is the slot
  cleared to `NULL` and the stale local dropped — that is a re-entrancy
  double-DECREF, and `Py_CLEAR` is the fix.

## Refcounts — new-reference balance

- **Ownership transfer is not a leak.** `*p_result = result` (out-parameter),
  `ctx->slots = new_slots` (context struct), `listrepr = tmp` (plain alias) and
  `value->_m_dict = (struct cached_m_dict){ .copied=copied }` (compound
  literal) all move the reference somewhere the function no longer owns it.
- **`Py_BuildValue`'s `N` code consumes its argument** even though the call
  itself returns a new reference: `Py_BuildValue("N(N)", iter, list)`.
- **`Py_SETREF(dst, src)` consumes `src`,** not just `dst`.
- **Module-lifetime statics.** A file-scope `static PyObject *Struct = NULL;`
  assigned in a module-exec function is process-lifetime by design.
- **`PySet_Discard` does not steal a reference.** It removes an element; a
  following `Py_DECREF` of the same variable is not a double-free. (This was a
  factual error in the toolkit's own `STEAL_REF_APIS` table.)
- **`PyModule_AddObject` steals only on success,** so `Py_DECREF(x)` inside
  `if (PyModule_AddObject(m, "x", x) < 0) { ... }` is required, not a
  double-free. Judge steal-then-drop by brace depth: a drop *nested deeper*
  than the steal is the steal's own failure branch.
- **A `goto` inside the variable's own NULL check is not a live error path.**
  `PyObject *c = PyCapsule_New(...); if (c == NULL) { goto error; }` — `c` is
  NULL at the label and cannot leak there.
- **A variable declared in a nested block is out of scope at a function-level
  cleanup label,** so it cannot leak there (`complex_richcompare`'s `sub_res`).
- **A cleanup label that `return`s the variable** is transferring ownership,
  not leaking (`math_fsum`'s `_fsum_error: ... return sum;`). Note CPython
  indents such labels, so a column-0 label regex misses them entirely.

## Reading CPython source with a scanner — two standing traps

- **Some markers only exist in comments.** The positional static `PyTypeObject`
  form names its slots in trailing comments (`(newfunc)list_new, /* tp_new */`)
  — 42 occurrences in `Objects/` versus 2 designated. Any check for slot
  registration must run on the *raw* source.
- **Some markers only exist in string literals.** `_ctypes`' re-init guard is
  the message `"StgInfo of '%s' is already initialized."`. A guard check run
  against comment-and-string-stripped source cannot see it.

## Refcounts — the escape / deref hazards (v0.9, `slot_transfer_across_call` and `stale_slot_use`)

- **Type-constrained operand makes a protocol call non-Python-reaching.**
  `PyNumber_Add` / `PyObject_RichCompare` / `PyObject_Hash` are all in
  `PYTHON_REACHING_APIS`, but if *every* operand is provably of a concrete
  builtin type for the lifetime of the field, the dispatch resolves to a C slot
  and no user code runs. `Objects/enumobject.c:196`
  `increment_longindex_lock_held` is the exemplar: `en->one` is
  `_PyLong_GetOne()` and `en_longindex` is only ever a `PyLong` (it arrives via
  `start = PyNumber_Index(start)`), so borrowing across `PyNumber_Add` is safe.
  This is the numeric-protocol analogue of the existing "statically-known type
  slot" entry.
  **Do not generalise it.** The sibling `Modules/itertoolsmodule.c`
  `count_nextlong` is textually identical — same transfer idiom, same comment
  wording — and *is* a reproduced heap-use-after-free, because `lz->long_step`
  comes from a constructor parameter that is only `PyLong_Check`-ed on the
  `fast_mode` path while `count_nextlong` is the *slow* path. The discriminator
  the scanner encodes: a parameter counts as type-pinned only when the function
  coerces it through an int-producing conversion **of itself**
  (`start = PyNumber_Index(start)`); receiving a default
  (`long_step = _PyLong_GetOne()`) proves nothing about what the caller passed.

- **A completed ownership transfer is not a stale borrow.** `func = v->func;
  v->func = NULL; PyObject_CallNoArgs(func);` (`Modules/_tkinter.c`
  `TimerHandler`) and `elem = it->root_element; /* steals a reference */
  it->root_element = NULL;` (`Modules/_elementtree.c` `elementiter_next`) clear
  the slot *before* anything can run Python, so the local is the legitimate
  sole owner. Only a clear a re-entrant call could reach — one at or after the
  first Python-reaching call — is dangerous.

- **A re-read of the slot after the call is the guarded twin.**
  `Modules/itertoolsmodule.c` `pairwise_next:364` calls
  `(*Py_TYPE(it)->tp_iternext)(it)`, then re-reads `it = po->it` and returns on
  NULL. A local that is re-loaded from its slot cannot be stale.

- **`#ifdef Py_GIL_DISABLED` asymmetry is a promotion signal, not a
  suppression.** The "never reason across a `#if`/`#else`" rule is about
  reasoning *across* arms. In `batched_next` the `Py_CLEAR(bo->it)` calls and
  the dangerous `iternext(it)` compile into the *same* (default, GIL) build;
  the `#ifndef Py_GIL_DISABLED` only tells you the bug is GIL-build-only — and
  the free-threaded arm is the fix. Report per-configuration, and treat "one
  arm guards, the other does not" as evidence *for* the finding.

- **A raw `PyMem_Malloc` buffer hanging off a live object is NOT protected by
  its owner** — a carve-out from the "borrowed ref under a known-live owner"
  entry above. That rule says "a *strong reference* is provably held for the
  duration"; a strong reference to the *container* says nothing about a
  refcount-less block hanging off it, which an ordinary method on the live
  receiver is free to `PyMem_Free`. Three reproduced instances:
  `Modules/_struct.c` `s_codes` (freed by `Struct.__init__` → `prepare_s`
  under an in-flight `pack`/`unpack`), `Modules/_zoneinfo.c`'s
  `StrongCacheNode` chain (freed by `ZoneInfo.clear_cache()` from inside a
  user `__eq__`), and `Modules/_elementtree.c`'s `extra`. None of the three is
  reachable by the scanner's rules — they walk the buffer with pointer
  arithmetic rather than caching it into one local — so this entry exists to
  stop a reader dismissing them.

## Free-threading — field synchronisation asymmetry (v0.9, T1 retarget)

Every entry was a measured false positive of the per-site T1 rule on CPython
main @ 3.16.0a0 and is now gated in the scanner.

- **The lock is in the Argument Clinic wrapper.** `@critical_section` in a
  `/*[clinic input]*/` block emits `Py_BEGIN_CRITICAL_SECTION` into
  `<dir>/clinic/<file>.c.h`, so the `_impl` body looks completely
  unsynchronised while every access in it runs under a per-object lock. This
  was the single largest FP class of the retargeted rule (`Modules/_io/`,
  `Modules/_collectionsmodule.c`).
- **The caller holds the section, transitively.** `count_nextlong`
  (`Modules/itertoolsmodule.c`) is not named `*_lock_held` and takes no lock,
  but its only free-threaded caller wraps it in `Py_BEGIN_CRITICAL_SECTION(lz)`.
  Chains are longer than one hop: `Modules/_io/textio.c` reaches
  `_textiowrapper_writeflush` from `textiowrapper_read_chunk`, itself reached
  only from clinic-guarded impls.
- **The lock is a macro.** `LOCK_WEAKREFS(obj)` / `UNLOCK_WEAKREFS_FOR_WR(self)`
  (`Include/internal/pycore_weakref.h`) expand to a critical section, and the
  `#define` is not in the `.c` file being scanned.
- **A by-value aggregate is never shared.** `WFILE wf;` in `Python/marshal.c`,
  `struct worklist` and `gc_mark_args_t` in `Python/gc_free_threading.c`: the
  caller owns it on its stack and passes `&wf`, so `p->buf` cannot race.
- **Teardown, fork-child and assert-only paths.** `_Py_qsbr_after_fork` runs in
  the single-threaded child; `_PyObject_ManagedDictValidityCheck` and the
  `*CheckConsistency` family compile out or run from a debugger;
  `*_dealloc`/`*_traverse`/`*_clear`/`*Finalize*`/`*destroy*` run when nothing
  else can observe the object.
- **`PyType_Ready` construction.** `inherit_slots` / `type_ready_*` populate
  slots before the type object is reachable, so a plain read of `tp_free`
  elsewhere is not racing them.
- **A guarded write into a freshly allocated object is not evidence.**
  `deque_copy_impl` stores `new_deque->maxlen` while holding the *source*
  deque's lock, and `deque_iter` fills a brand-new iterator. Neither makes a
  plain read of that field elsewhere a race — such writes are excluded from the
  guarded-twin set, not just from the finding set.
- **A field with many unguarded accessors is an un-hardened module, not a
  missed guard.** `Modules/_pickle.c` has three critical sections in 8,298
  lines and no per-object locking on `Pickler`/`Unpickler` state at all;
  reporting each site as a separate FIX misstates the problem. The scanner caps
  T1 at 4 unsynchronised sites across 2 functions per field for exactly this
  reason — report the wholesale case as one POLICY finding instead.

## Error paths — `int_status_never_tested` (issue #28 rule 4)

- **Symmetric cleanup that must run regardless of the status.**
  `Modules/_pickle.c` `save_frozenset:3796` is the shape:

  ```c
  if (self->fast && !fast_save_enter(self, obj)) { return -1; }
  int status = save_frozenset_impl(state, self, obj);
  if (self->fast && !fast_save_leave(self, obj)) { return -1; }
  return status;
  ```

  The scanner's "the region between the assignment and the read is fallible"
  gate fires on that intervening `return -1;`, but the intervening call is the
  *leave* half of an enter/leave pair and is **required** to run on the failure
  path too. Nothing is committed and nothing is skipped, and `status` reaches
  the caller unchanged. Tell this apart from the real shape by asking what the
  intervening code *does*: cleanup that must happen either way is correct,
  whereas `type_set_bases_unlocked:1966` runs `update_all_slots()` with a live
  exception and skips its own rollback. **1 of the 2 candidates tree-wide is
  this class** — check it first.
- **Accumulate-then-return.** `res = f(); Py_DECREF(x); return res;` with only
  cleanup in between is correct and is already suppressed by the same gate
  (160 raw assignments across `Objects/` + `Modules/` + `Python/` reduce to 2).

## NULL checks — the widened fallible-source set (issue #28)

`scan_null_checks` used to resolve its assignment sources from a closed enum of
45 API names, which reached **49 of 760** assignment-from-call sites (6.4%).
The producer of the value is now also discovered from the file: any
pointer-returning function whose body can `return NULL`, transitively through
thin forwarders. That is what made `Objects/dictobject.c:4494` visible at all.
Two FP classes come with it:

- **A public API that is both a checked function and an unchecked macro in the
  same translation unit.** `Objects/unicodeobject.c:15388` defines
  `void* PyUnicode_DATA(PyObject *op)` with a real `return NULL` type-check
  path — so discovery is correct to call it fallible — but every *internal*
  call site expands the same-named **macro** instead, which cannot fail. The
  finding at `:12922` is real about the name and wrong about the call. Check
  whether the header defines a macro of the same name before triaging.
- **The argument is provably the right type at the call site.** Most of these
  helpers only return NULL on a type check the caller has already done. That is
  a legitimate dismissal, but say *which* prior check establishes it.

Before dismissing a whole file, read the three denominators now in the
envelope: `assignment_sites`, `fallible_sources_resolved`, and
`local_nullable_helpers`. Tree-wide they are 78,109 / 5,386 / 5,341.
`summary.decref_of_nulled_outparam_call_sites` exists for the same reason: that
rule's denominator on CPython is **effectively zero**, so its zero is
structural and must never be reported as a clean result.

## Dynamic verification — artifacts of the harness, not of CPython (obj-typeobject pass 2)

Three of these cost real time in one run. All three produce a *confident wrong
answer*, which is what makes them worth naming.

- **A deprecated API in the stress script imports a module on a worker
  thread.** `scenario_mixed` hung under TSan and then reproduced **5/6 on plain
  FT vs 0/4 on GIL** — a textbook new-finding signature. The cause was
  `sys._clear_type_cache()` being **deprecated**: emitting its warning formats
  via `linecache`, which lazily imports `tokenize` → `io` → `ABCMeta.__new__`,
  i.e. **class creation on a worker thread**, starved by four other threads
  stopping the world. Suppressing the warning: **0/6**. Before believing an
  FT-only hang in a stress script, check whether any call in it is deprecated
  or triggers a lazy import. Note the honest limit: this data does **not**
  settle starvation vs. true deadlock — it only shows the trigger is the
  harness.
- **A partial TSan log is indistinguishable from a clean one.** TSan writes
  race blocks as it goes, so a run still executing looks exactly like a run
  that found nothing. This misled the same session **three times**: two
  scenarios read 0 races mid-run and finished at 29 and 22, and the third
  nearly cost the second confirmation of a real finding. Never conclude from a
  log whose process is still alive.
- **`os.fork()`-per-scenario isolation deadlocks under TSan.** The stress-agent
  template's default isolation cannot be used on a TSan build. Use
  `STRESS_NO_FORK=1` with per-process scenario selection from a driver script.

## Guarded twins are twin *for a specific threat model*

The informed-explore method leans on "name the guarded twin — that is the fix".
One correction, from `obj-typeobject` pass 2: `_PyType_GetSubclasses:793` was
cited as a guarded twin of the `_PyType_Modified_Unlocked` cursor-invalidation
UAF, on the strength of an explicit in-code comment justifying its borrowed
reference. **That comment addresses re-entrancy, not concurrent mutation** —
and under the second threat model the "twin" is itself a defect (reproduced
cross-thread: `T.__subclasses__()` racing `X.__bases__ = (...)`, against
`init_tp_subclasses:702` replacing the dict and `delitem_common:2987` deleting
from it).

Before citing a site as the twin, read *what its comment actually claims* and
confirm the claim covers the threat model you are reasoning about. A site can
be simultaneously the correct model for re-entrancy and a live bug for
concurrency.
