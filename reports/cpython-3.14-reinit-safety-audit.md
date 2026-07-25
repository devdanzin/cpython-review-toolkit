# CPython 3.14 tp_init / tp_new Safety Audit

**Date**: 2026-03-29
**Scope**: All C source files with `tp_init` or `tp_new` across `Modules/`, `Objects/`, and `Python/`
**Method**: Automated script scan (`scan_refcounts.py`) + 6 parallel agent deep-reviews in two rounds

---

## Executive Summary

Audited **80+ types** across 70+ C source files. Found:
- **8 confirmed tp_init re-init vulnerabilities** (calling `__init__()` twice leaks resources)
- **3 tp_new uninitialized-member issues** (non-zeroing allocator without NULL init)
- **5 types needing review** for logic/side-effect/minor issues

The agent reviews found **all 8 re-init vulnerabilities** — the scanner caught 0 of the actual tp_init issues due to Clinic `_impl` names, `#ifdef` brace imbalance, helper-function indirection, and OS resource patterns.

---

## Part 1: tp_init Re-Init Vulnerabilities (FIX)

### 1. `xml.etree.ElementTree.XMLParser` — Severe multi-resource leak
**File**: `Modules/_elementtree.c`, `_elementtree_XMLParser___init___impl` (line 3701)
**What leaks**: 2 Python dicts (`entity`, `names`), native expat parser handle + all its internal memory, target object reference, ~10 `handle_*` method references
**Detail**: All member assignments are plain `self->foo = ...` without any cleanup of prior values. Most severe finding — a single re-init leaks a native parser, 2 dicts, and 10+ Python object references.
**Fix**: Add cleanup block: `Py_CLEAR(self->entity); Py_CLEAR(self->names); if (self->parser) EXPAT(ParserFree)(self->parser); Py_CLEAR(self->target);` etc.

### 2. `_remote_debugging.RemoteUnwinder` — OS handle + hashtable leak
**File**: `Modules/_remote_debugging_module.c`, `_remote_debugging_RemoteUnwinder___init___impl` (line 2558)
**What leaks**: OS process handle, `code_object_cache` hashtable + all cached entries, `tlbc_cache` (on free-threaded builds)
**Detail**: Unconditionally overwrites `self->handle`, `self->code_object_cache`, and `self->tlbc_cache` without freeing previous values.
**Fix**: Add cleanup block mirroring `RemoteUnwinder_dealloc` logic at top of `__init__`.

### 3. `socket.socket` — File descriptor leak
**File**: `Modules/socketmodule.c`, `sock_initobj_impl` (line 5623)
**What leaks**: OS socket file descriptor
**Detail**: Creates a new socket FD and calls `init_sockobject()` → `set_sock_fd(s, fd)`, overwriting `s->sock_fd` without closing the prior FD.
**Fix**: Check `if (s->sock_fd != INVALID_SOCKET)` and close it, or reject re-init.

### 4. `_io.BufferedRWPair` — Python object reference leak
**File**: `Modules/_io/bufferedio.c`, `_io_BufferedRWPair___init___impl` (line 2259)
**What leaks**: `self->reader` (BufferedReader) and `self->writer` (BufferedWriter)
**Detail**: Assigns without `Py_CLEAR` on previous values. Sister types BufferedReader/Writer/Random are safe.
**Fix**: Add `Py_CLEAR(self->reader); Py_CLEAR(self->writer);` before new assignments.

### 5. `xml.etree.ElementTree.Element` — Memory + object leak
**File**: `Modules/_elementtree.c`, `element_init` (line 413)
**What leaks**: `ElementObjectExtra` struct (heap), its `attrib` dict reference, children array
**Detail**: `create_extra()` does `self->extra = PyMem_Malloc(...)` unconditionally without checking/freeing existing `self->extra`.
**Fix**: Free existing `extra` before `create_extra()`.

### 6. `_lsprof.Profiler` — Python object reference leak
**File**: `Modules/_lsprof.c`, `profiler_init_impl` (line 1005)
**What leaks**: `self->missing` (one reference per re-init)
**Detail**: `self->externalTimer` uses `Py_XSETREF` (safe), but `self->missing = PyObject_GetAttrString(...)` overwrites without decref.
**Fix**: Use `Py_XSETREF(self->missing, PyObject_GetAttrString(...))`.

### 7. `SyntaxError` — Multiple reference leaks
**File**: `Objects/exceptions.c`, `SyntaxError_init` (line 2648)
**What leaks**: `self->filename`, `self->lineno`, `self->offset`, `self->text`, `self->end_lineno`, `self->end_offset`
**Detail**: When called with 2 args, `PyArg_ParseTuple` writes borrowed references directly into struct fields with bare `Py_INCREF`, overwriting without decrementing old values. Also sets `end_lineno`/`end_offset` to NULL without `Py_XDECREF`.
**Fix**: Use `Py_XSETREF` for all field assignments, or `Py_CLEAR` before overwriting.

### 8. `OSError` (subclasses) — Multiple reference leaks
**File**: `Objects/exceptions.c`, `oserror_init` (line 1988)
**What leaks**: `self->filename`, `self->filename2`, `self->myerrno`, `self->strerror` (and `self->winerror` on Windows)
**Detail**: Uses bare `self->filename = Py_NewRef(filename)` without `Py_XSETREF`. Only affects OSError subclasses where `oserror_use_init()` returns true (custom `__init__` with default `__new__`); plain `OSError` is not affected.
**Fix**: Replace `Py_NewRef`/`Py_XNewRef` assignments with `Py_XSETREF`.

---

## Part 2: tp_new Uninitialized Member Issues (CONSIDER)

### 9. `sha3` types — Uninitialized `hash_state` before GC tracking
**File**: `Modules/sha3module.c`, `newSHA3object` (line 74)
**Allocator**: `PyObject_GC_New` (non-zeroing)
**Detail**: `hash_state` (pointer) is not initialized to NULL before `PyObject_GC_Track` at line 82. If the caller's error path is reached before `hash_state` is assigned, `SHA3_clear()` tests an uninitialized pointer with `if (self->hash_state != NULL)`, potentially calling `Hacl_Hash_SHA3_free()` on garbage.
**Fix**: Add `self->hash_state = NULL;` in `newSHA3object()`.

### 10. `_dbmmodule` — Uninitialized `di_dbm` before GC tracking
**File**: `Modules/_dbmmodule.c`, `newdbmobject` (line 81)
**Allocator**: `PyObject_GC_New` (non-zeroing)
**Detail**: `di_dbm` is not set to NULL before `PyObject_GC_Track` at line 87. If `dbm_open` fails, `dbm_dealloc` tests `if (dp->di_dbm)` against uninitialized memory. Not reachable via `__new__` (DISALLOW_INSTANTIATION) but still a correctness bug.
**Fix**: Add `dp->di_dbm = NULL;` before `PyObject_GC_Track`.

### 11. `_gdbmmodule` — Same pattern as _dbmmodule
**File**: `Modules/_gdbmmodule.c`, `newgdbmobject` (line 103)
**Detail**: Identical issue — `di_dbm` uninitialized before GC tracking.
**Fix**: Add `dp->di_dbm = NULL;` before `PyObject_GC_Track`.

---

## Part 3: Needs Review (CONSIDER)

### 12. `asyncio.Task` — Side-effect accumulation on re-init
**File**: `Modules/_asynciomodule.c`, `_asyncio_Task___init___impl` (line 2327)
**Issue**: Field management is safe (`Py_XSETREF`, `Py_CLEAR`). But `register_task(self)` is called unconditionally — re-init double-registers. Step scheduling could also produce logic corruption.

### 13. `xml.etree.ElementTree.TreeBuilder` — Stale parsing state on re-init
**File**: `Modules/_elementtree.c`, `_elementtree_TreeBuilder___init___impl` (line 2459)
**Issue**: Factory fields use `Py_XSETREF`/`Py_CLEAR` (safe). But `root`, `this`, `last`, `data`, `stack`, `index` are NOT reset.

### 14. `sqlite3.Cursor` — Weakref accumulation on re-init
**File**: `Modules/_sqlite/cursor.c`, `pysqlite_cursor_init_impl` (line 98)
**Issue**: Fields use `Py_XSETREF`/`Py_CLEAR` (safe). But `register_cursor` appends a new weakref without removing previous entry.

### 15. `blake2` types — Minor: GC tracking before state pointer init
**File**: `Modules/blake2module.c`, `new_Blake2Object` (line 384)
**Issue**: Same pattern as sha3 but documented in code comments (line 980-983). Window between GC_Track and NULL-init is narrower. Low risk.

### 16. `dict.__init__` — Merges rather than replaces
**File**: `Objects/dictobject.c`, `dict_init` (line 4940)
**Issue**: Not a leak, but `d.__init__({new: data})` merges new items into existing dict without clearing old keys first. This is by design but worth noting — it's different from `d = dict({new: data})`.

---

## Safe Types Summary

The vast majority of types audited are safe. Key safe patterns observed:

| Pattern | Example types | Count |
|---------|--------------|-------|
| No tp_init (all init in tp_new) | datetime, itertools, enumerate, reversed, cell, memoryview | ~25 |
| Immutable types (tp_init is no-op) | int, float, str, bytes, tuple, range, slice, bool | ~10 |
| Explicit re-init guard (`self->initialized`) | sqlite3.Connection | 1 |
| Py_XSETREF/Py_CLEAR cleanup | property, super, classmethod, staticmethod, ImportError, AttributeError, NameError, StopIteration, SystemExit, Unicode*Error, list, set, bytearray, module, Struct, Pickler, Unpickler, Future, deque, defaultdict | ~20 |
| DISALLOW_INSTANTIATION (no tp_new) | hash types, sre types, curses, csv reader/writer, _ssl internal types | ~20 |
| Zeroing allocator (tp_alloc) | most types with exposed tp_new | ~35 |

---

## Scanner vs Agent Effectiveness

### tp_init re-init detection

| Category | Scanner | Agent |
|----------|---------|-------|
| Confirmed vulnerabilities found | 0/8 | 8/8 |
| False positives | 4 (ctypes/io had guards scanner missed) | 0 |
| Module-level inits (not tp_init) | 5 (valid but different scope) | N/A |

### tp_new uninitialized-member detection

| Category | Scanner | Agent |
|----------|---------|-------|
| Issues found | 3 (ctxmanager_new, PicklerMemoProxy, UnpicklerMemoProxy) | 3 (sha3, _dbm, _gdbm) |
| Scanner FPs | 3 (all safe on closer inspection — members initialized before use) | 0 |
| Agent found scanner missed | — | sha3, _dbm, _gdbm (GC-track before init) |

### Why the scanner misses tp_init issues
1. **Clinic `_impl` suffixes** with complex names don't match `_init$` regex
2. **`#ifdef` brace imbalance** prevents function body extraction (socketmodule)
3. **Helper-function indirection** — allocation in `create_extra()`, not directly in init
4. **OS resources** (FDs, handles, expat parsers) aren't in the Python allocation API list
5. **Non-`self` parameter names** partially addressed but still edge cases

### Scanner strengths
- Sub-second broad sweep across hundreds of files
- Good at finding module-level init patterns
- Useful as first-pass triage before agent review

---

## Recommendations

### Immediate (FIX — 8 vulnerabilities)
1. **XMLParser** — most severe, leaks native parser + many objects
2. **RemoteUnwinder** — leaks OS process handle + hashtables
3. **SyntaxError** — leaks exception fields, common exception type
4. **OSError subclasses** — leaks exception fields
5. **socket.socket** — leaks FDs, extremely high-traffic type
6. **BufferedRWPair** — leaks reader/writer objects
7. **Element** — leaks extra struct + attrib dict
8. **Profiler** — single ref leak per re-init

### Short-term (CONSIDER — 3 tp_new issues + 5 review items)
9. **sha3 hash_state** — one-line NULL init fix
10. **_dbmmodule di_dbm** — one-line NULL init fix
11. **_gdbmmodule di_dbm** — one-line NULL init fix
12-16. Task, TreeBuilder, Cursor, blake2, dict — review for logic/side-effect issues
