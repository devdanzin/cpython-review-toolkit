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

- **Guarded by the dispatcher.** A leaf slot reached only through
  `PyObject_Repr`/`PyObject_Hash`/`PyObject_RichCompare`, which already wrap
  `Py_EnterRecursiveCall`, is safe *if it is never reached directly*. Confirm the
  call graph before dismissing.
- **Non-nestable receiver.** A `*_hash`/`*_repr` on a type whose elements can
  never be the same or another container (e.g. a code object's fixed fields) can
  descend without a guard because the depth is bounded. ACCEPTABLE — but state
  the bound.
- **`Py_TRASHCAN` / iterative deallocation** already bounds a `tp_dealloc`; a
  recursion finding on a trashcan-protected dealloc is a FP.

## Uninitialized dealloc

- **Zeroing allocator.** `type->tp_alloc(type, n)` / `PyType_GenericAlloc` /
  `*_GC_Calloc` zero the object; a following early free is safe. The scanner
  excludes these, but a wrapper macro may hide one.
- **`tp_dealloc` guards each member with `Py_XDECREF`** *and* the members were
  NULL-initialized before the failing step — Py_XDECREF(NULL) is a no-op, so no
  crash. Only a member left as *garbage* (not NULL) at the free point is a bug.

## NULL checks / error paths

- **Infallible-by-construction returns.** `Py_None`/`Py_True`/`Py_False`, interned
  singletons, and `_Py_ID(...)` never return NULL; a missing check is not a bug.
- **Checked via a macro the scanner doesn't model.** `Py_SETREF` / `Py_XSETREF`
  and `Py_CLEAR` handle NULL internally; assignments through them are safe.

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
