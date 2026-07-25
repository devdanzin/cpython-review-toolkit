# CPython main (3.15-dev) Git History Analysis

**Date**: 2026-03-29
**Branch**: `main` at `~/projects/ft_cpython`
**Period**: October 2024 — March 2026 (3 x 6-month windows, parallel agents)
**Total commits scanned**: ~2,866 C-file commits
**Fixes reviewed in detail**: 30 (10 per window)
**Similar bug searches**: 15 (5 per window)

---

## Actionable Findings

### HIGH Confidence Similar Bugs

| # | Bug Pattern | Original Fix | Unfixed Location | New in 3.15? |
|---|------------|-------------|-----------------|-------------|
| 1 | Missing `Py_VISIT` in traverse (clear visits it, traverse doesn't) | ctypes StructParam_traverse (9c4fb92) | `_ctypes/cfield.c:410` — `PyCField_traverse` missing `Py_VISIT(self->name)` | No |
| 2 | Missing `Py_VISIT` in traverse | ctypes StructParam_traverse (9c4fb92) | `_zstd/decompressor.c:687` — `ZstdDecompressor_traverse` missing `Py_VISIT(self->unused_data)` | **Yes** |
| 3 | NULL from dead weak reference | SSL SNI callback (24db78c) | `_ssl/debughelpers.c:32-38` — `PyWeakref_GetRef` returns NULL, only guarded by assert (disabled in release) | No |
| 4 | Reference leak on early return | Compressor/decompressor divergence | `_zstd/decompressor.c:113` — `value` not DECREF'd when `PyLong_AsInt(key)` fails (compressor.c does it correctly) | **Yes** |

### MEDIUM Confidence Findings

| # | Pattern | Location | Detail |
|---|---------|----------|--------|
| 5 | Inconsistent error handling in new module | `_remote_debugging/*.c` | Mixed error reporting (PyErr_Format + set_exception_cause vs bare return -1). Module already had a major error-handling fix (ae6adc9). |
| 6 | `get_asyncio_state_by_def()` in repr methods | `_asynciomodule.c:1654, 2534` | `FutureObj_repr` and `TaskObj_repr` call `get_asyncio_state_by_def` which asserts `mod != NULL` — repr can be called during finalization when module is gone. |

### Incomplete Fix

| # | Fix | What Was Missed |
|---|-----|----------------|
| 7 | SSL SNI callback crash (24db78c) | `_ssl/debughelpers.c:32-38` has the identical pattern — `PyWeakref_GetRef` can return NULL but only an assert guards it. The fix addressed `_servername_callback` but not `_PySSL_msg_callback`. |

---

## Fix Quality by Period

| Period | Fixes Reviewed | ACCEPTABLE | CONSIDER | FIX | Similar Bugs (HIGH) |
|--------|---------------|------------|----------|-----|-------------------|
| Oct 2024 – Mar 2025 | 10 | 9 | 1 | 0 | 2 |
| Mar – Sep 2025 | 10 | 9 | 1 | 0 | 1 |
| Sep 2025 – Mar 2026 | 10 | 9 | 0 | 1 | 2 |
| **Total** | **30** | **27** | **2** | **1** | **5** |

---

## What's Different on main vs 3.14

### New 3.15 modules are the richest bug source

| Module | Commits | Bugs Found | Pattern |
|--------|---------|-----------|---------|
| `_zstd` | 36 | 3 (traverse/visit, ref leak, copy-paste) | Compressor/decompressor divergence from copy-paste |
| `_remote_debugging` | 31 | Inconsistent error handling | New module growing pains |
| t-strings (`templateobject.c`) | 10 | 2 (exhausted iterator crash, arena leak) | New feature with edge cases |
| `annotationlib` | 19 | Under review | New feature |

### Fix-to-feature ratio tells the story

| Period | Branch | Ratio | Phase |
|--------|--------|-------|-------|
| Oct 2024 – Mar 2025 | main | 1.0:1 | Active development (features = fixes) |
| Mar – Sep 2025 | main | 1.0:1 | Continued active development |
| Sep 2025 – Mar 2026 | main | 3.25:1 | Stabilization before alpha |
| Same period | 3.14 | 7.1:1 | Late RC / release |

### Dominant bug categories unique to main

1. **Traverse/clear mismatches in new modules** — The `_zstd` and `_ctypes` traverse functions are missing `Py_VISIT` calls for members that `_clear` handles. This is a pattern that's easy to introduce when adding new struct members.

2. **Copy-paste divergence** — `_zstd` compressor and decompressor were developed in parallel with copy-paste. The decompressor has at least 2 bugs (ref leak, wrong error message) that the compressor doesn't, because fixes were applied to one but not the other.

3. **Weak reference NULL safety** — The SSL module's weak reference pattern (`PyWeakref_GetRef` returning NULL for dead objects) was fixed in one callback but not its sibling.

4. **JIT optimizer edge cases** — 134 commits to optimizer files, with fixes for out-of-space conditions in JIT symbol tables and func_version handling bugs.

---

## Recommendations

### Immediate (4 HIGH-confidence bugs)

1. **`_zstd/decompressor.c:687`** — Add `Py_VISIT(self->unused_data)` to `ZstdDecompressor_traverse`. One-line fix. Brand new 3.15 module.

2. **`_zstd/decompressor.c:113`** — Add `Py_DECREF(value)` before `return -1` when `PyLong_AsInt(key)` fails. The compressor already does this correctly at `compressor.c:158`.

3. **`_ssl/debughelpers.c:32-38`** — Replace `assert(ssl_socket != NULL)` with a proper NULL check and early return. The `_servername_callback` fix (24db78c) addressed this pattern but missed this sibling.

4. **`_ctypes/cfield.c:410`** — Add `Py_VISIT(self->name)` to `PyCField_traverse`. Same traverse/clear mismatch as the fixed StructParam_traverse.

### Short-term

5. **Audit all `_zstd` code for compressor/decompressor divergence** — systematic comparison of the two files to catch remaining copy-paste bugs.

6. **Audit `_remote_debugging` error paths** — the module has already had one major error-handling fix and the remaining code has inconsistent patterns.

7. **Check `asyncio` repr methods for finalization safety** — `FutureObj_repr` and `TaskObj_repr` access module state that may be NULL during interpreter shutdown.

### Systemic

8. **Add a traverse/clear consistency check to the toolkit** — The pattern "clear() handles a member but traverse() doesn't visit it" is a reliable bug indicator. Could be automated in `scan_refcounts.py`.
