# CPython 3.14 Git History Analysis — 2 Year Sweep

**Date**: 2026-03-29
**Period**: March 2024 — March 2026 (4 x 6-month windows, parallel agents)
**Total commits scanned**: ~6,000 C-file commits, ~1,039 classified as fixes
**Fixes reviewed in detail**: 40 (10 per window)
**Similar bug searches**: 20 (5 per window)

---

## Actionable Findings

### Similar Bugs Found (unfixed code with the same pattern as a fix)

| # | Bug Pattern | Original Fix | Unfixed Location | Confidence |
|---|------------|-------------|-----------------|------------|
| 1 | Re-entrant `__eq__` use-after-free: `PyObject_RichCompareBool` on struct members without INCREF | `groupby_next` (13b3dd0) | `_grouper_next` in `itertoolsmodule.c:681` — same iterator, same unprotected comparison | HIGH |
| 2 | Unsynchronized lazy-init data race | `sre.c compile_template` (3cf68cd) | `arraymodule.c:2360` — `state->array_reconstructor` check-then-assign, module declares `Py_MOD_GIL_NOT_USED` | HIGH |
| 3 | Unsynchronized lazy-init data race | `sre.c compile_template` (3cf68cd) | `_decimal.c:3492` — `state->PyDecimal` check-then-assign, module declares `Py_MOD_GIL_NOT_USED` | HIGH |
| 4 | Unsynchronized lazy-init data race + ref leak | `sre.c compile_template` (3cf68cd) | `_functoolsmodule.c:105` — `state->placeholder` check-then-assign, two threads can both pass check and leak a ref | HIGH |
| 5 | Missing `PyErr_NoMemory()` after `PyMem_Malloc` failure | `_zoneinfo` (a2a45d7) | `_ctypes/_ctypes.c:670-672` — `StructUnionType_paramfunc` returns NULL without setting exception | MEDIUM |
| 6 | `module_traverse`/`module_clear` discards return value | `_interpqueuesmodule.c` et al (6e5350d) | `_testinternalcapi.c:2662-2677` — same pattern, 4th sibling module unfixed | HIGH |
| 7 | `Task.get_context()` missing NULL check | `Task.get_coro` (c086962) | `_asynciomodule.c:2816` — `Py_NewRef(self->task_context)` without NULL check, `task_context` cleared by `TaskObj_clear` | MEDIUM |

### Potentially Incomplete Fixes

| # | Fix | What May Be Missed | Confidence |
|---|-----|-------------------|------------|
| 1 | `groupby_next` re-entrancy (13b3dd0) | `_grouper_next` in same file not patched | HIGH |
| 2 | `sre.c` lazy-init race (3cf68cd) | 3 other modules with identical pattern not patched | HIGH |
| 3 | `_interp*` module_traverse return (6e5350d) | `_testinternalcapi.c` not patched | HIGH |
| 4 | Warnings finalization ordering (d619015) | Other `_Fini` calls before final GC could have similar issues | MEDIUM |

---

## Fix Quality Assessment by Period

| Period | Fixes Reviewed | ACCEPTABLE | CONSIDER | FIX | Similar Bugs Found |
|--------|---------------|------------|----------|-----|-------------------|
| Mar–Sep 2024 | 10 | 9 | 1 | 0 | 1 |
| Sep 2024–Mar 2025 | 10 | 9 | 1 | 0 | 3 |
| Mar–Sep 2025 | 10 | 8 | 2 | 0 | 1 |
| Sep 2025–Mar 2026 | 10 | 8 | 2 | 0 | 2 |
| **Total** | **40** | **34** | **6** | **0** | **7** |

Fix quality is consistently high across all periods — 85% of fixes reviewed are complete and correct. No fix was classified as definitively incomplete (FIX), though 6 had aspects worth review (CONSIDER).

---

## Dominant Bug Categories by Period

| Period | Top Category | Examples |
|--------|-------------|---------|
| Mar–Sep 2024 | Reference counting | 33 refcount leak fixes, 36 NULL/crash fixes |
| Sep 2024–Mar 2025 | Free-threading races | `memset` over-zeroing, `FreeList_Push` race, lazy-init races |
| Mar–Sep 2025 | Subinterpreter safety | Context init, module traverse/clear, type versioning |
| Sep 2025–Mar 2026 | Re-entrancy bugs | groupby, partial_repr, array.__setitem__, BytesIO |

**Trend**: The bug class shifted from classical refcount issues (2024) to free-threading and re-entrancy (2025-2026), reflecting CPython 3.14's major focus on PEP 703 (free-threading).

---

## Fix-to-Feature Ratio Trend

| Period | Fix:Feature | Interpretation |
|--------|------------|---------------|
| Mar–Sep 2024 | 3.2:1 | Active development, alpha phase |
| Sep 2024–Mar 2025 | 4.9:1 | Stabilization, approaching beta |
| Mar–Sep 2025 | 2.7:1 | RC phase stabilization |
| Sep 2025–Mar 2026 | 7.1:1 | Late RC / release, heavy fix mode |

---

## High-Churn Files (across all periods)

These files had the most fix commits across the 2-year window:

| File | Fix Commits | Primary Bug Category |
|------|------------|---------------------|
| `Objects/typeobject.c` | 25+ | Type system + free-threading |
| `Python/bytecodes.c` | 22+ | Instruction execution + STACKREF |
| `Python/compile.c` | 20+ | Compilation edge cases |
| `Objects/dictobject.c` | 16+ | Dict internals + free-threading |
| `Modules/_asynciomodule.c` | 12+ | Task lifecycle + NULL safety |
| `Modules/_pickle.c` | 8+ | Error path refcount leaks |
| `Modules/_decimal/_decimal.c` | 6+ | GC tracking + context management |
| `Modules/itertoolsmodule.c` | 5+ | Re-entrancy safety |

---

## Module Family Propagation Gaps

| Fix | Fixed Module | Unfixed Siblings |
|-----|-------------|-----------------|
| Lazy-init race (3cf68cd) | `_sre/sre.c` | `arraymodule.c`, `_decimal.c`, `_functoolsmodule.c` |
| module_traverse return (6e5350d) | `_interpqueuesmodule.c`, `_interpretersmodule.c`, `_interpchannelsmodule.c` | `_testinternalcapi.c` |
| MD5 dealloc NULL guard (c23dd527) | `md5module.c` | All siblings already fixed (sha1, sha2, sha3, blake2) |

---

## Recommendations

### Immediate (7 similar bugs, all actionable)

1. **`_grouper_next` re-entrancy** (itertoolsmodule.c:681) — HIGH confidence, same use-after-free pattern as the groupby fix. Add INCREF protection for `igo->tgtkey` and `gbo->currkey` before `PyObject_RichCompareBool`.

2. **Lazy-init races in 3 modules** (arraymodule.c:2360, _decimal.c:3492, _functoolsmodule.c:105) — HIGH confidence, all declare `Py_MOD_GIL_NOT_USED`. Need `FT_ATOMIC_LOAD_PTR` + `_Py_atomic_compare_exchange_ptr` pattern from the sre.c fix.

3. **`_testinternalcapi.c` module_traverse/module_clear** (lines 2662-2677) — HIGH confidence, return value discarded. Even though it's test infrastructure, it's the same bug class.

4. **`Task.get_context()` NULL check** (_asynciomodule.c:2816) — MEDIUM confidence, same pattern as the get_coro fix.

5. **ctypes `StructUnionType_paramfunc` missing `PyErr_NoMemory`** (_ctypes.c:670-672) — MEDIUM confidence.

### Systemic

6. **Audit all `Py_MOD_GIL_NOT_USED` modules for lazy-init patterns** — the sre.c fix was not propagated to other modules. A grep for `if (state->.*== NULL).*=.*PyImport` in modules declaring `Py_MOD_GIL_NOT_USED` would find more.

7. **Audit itertools for re-entrancy** — the groupby fix protected one function but not its sibling. Other itertools types with user-defined callbacks (`key=` functions, `__eq__` comparisons) may have similar issues.
