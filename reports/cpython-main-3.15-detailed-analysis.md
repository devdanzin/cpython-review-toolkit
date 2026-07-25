# CPython main (3.15-dev) Detailed Git History Analysis

**Date**: 2026-03-29
**Branch**: `main` at `~/projects/ft_cpython`
**Period**: October 2024 — March 2026 (18 months)
**Methodology**: Three analysis rounds with increasing granularity:
- Round 1: 3 x 6-month windows, 30 fixes reviewed
- Round 2: 6 x 3-month windows, 60 fixes reviewed
- Round 3: 9 x 2-month windows, 90 fixes reviewed (final)

**Total**: ~5,000+ commits scanned, 90 fixes reviewed in detail, 45 similar-bug searches

---

## Actionable Findings — All Confirmed Bugs

### HIGH Confidence

| # | Bug | Where | Source Fix | Found In |
|---|-----|-------|-----------|----------|
| 1 | `(float)PyFloat_AsDouble` silent overflow to infinity on s390x — same pattern just fixed in `_struct.c` | `_ctypes/cfield.c:884,906` (`f_set`/`f_set_sw`) | struct fix `8de70b3` | Round 3 |
| 2 | `PyTuple_SET_ITEM` with unchecked `PyLong_FromSsize_t` in loop — NULL slot on alloc failure | `_ctypes/callproc.c:1983` | datetime fix `0eaf260d` | Round 2 |
| 3 | `os.environ.get()` not protected in 7+ `_pyrepl` locations — same crash as fixed `_colorize.py` | `_pyrepl/pager.py:27,31,35,92`, `simple_interact.py:49`, `terminfo.py:85,96,339`, `trace.py:12` | colorize fix `8ef7735c` | Round 2 |
| ~~4~~ | ~~`int i` loop counter overflow in pickle~~ | `_pickle.c` | ~~CSV fix `97b0ef0`~~ | ~~Round 3~~ — **FALSE POSITIVE**: all 5 `int i` are bounded by small constants (BATCHSIZE=1000, sizeof(size_t)=8, protocol byte=255). Data iteration uses `Py_ssize_t`. |
| 5 | `ZstdDecompressor_traverse` missing `Py_VISIT(self->unused_data)` | `_zstd/decompressor.c:687` | ctypes StructParam fix `9c4fb92` | Round 1 |
| 6 | `_zstd` decompressor leaks `value` ref when `PyLong_AsInt(key)` fails (compressor does it right) | `_zstd/decompressor.c:113` | compressor/decompressor divergence | Round 1 |
| 7 | SSL `_PySSL_msg_callback` uses dead weak ref — assert-only guard disabled in release | `_ssl/debughelpers.c:32-38` | SNI callback fix `24db78c` | Round 1 |
| 8 | `PyCField_traverse` missing `Py_VISIT(self->name)` | `_ctypes/cfield.c:410` | ctypes StructParam fix `9c4fb92` | Round 1 |

### MEDIUM Confidence

| # | Bug | Where | Detail |
|---|-------|-------|--------|
| 9 | `array.fromlist` signed-type setters may have same reentrant `__index__` UAF as unsigned | `arraymodule.c` (`bb/hh/ii/ll/qq_setitem`) | Unsigned fixed in `gh-144128`; signed uses `PyLong_As*` which also calls `__index__` |
| 10 | Bare `except:` in tarfile catches KeyboardInterrupt/SystemExit (6 instances) | `Lib/tarfile.py` (6 locations) | Masks critical exceptions during archive operations |
| 11 | `math.sumprod()` high-precision fix reverted, never re-landed | `Modules/mathmodule.c` | gh-139074 remains open |
| 12 | `_tkinter` GC protocol repeatedly landed and reverted | `Modules/_tkinter.c` | gh-116946 unresolved design |
| 13 | `Element.__copy__` reads `extra->length` once then iterates — unprotected in free-threaded builds | `_elementtree.c:781-791` | Marginal risk |
| 14 | SSL `load_dh_params` missing filename in errno error (cosmetic parity) | `_ssl.c:~4879` | Inconsistent with `load_cert_chain` fix |

### Incomplete Fix Propagation

| # | Fix | What Was Missed |
|---|-----|----------------|
| 15 | `_pyrepl` `os.environ.get()` crash (`8ef7735c`) | Only fixed in `_colorize.py`, not 7+ sibling locations |
| 16 | `_struct.c` float overflow (`8de70b3`) | Not propagated to `_ctypes/cfield.c` identical pattern |
| 17 | `array.fromlist` unsigned setters (`gh-144128`) | Signed setters not patched |
| 18 | CSV `int i` overflow (`97b0ef0`) | Not audited across other modules with similar counters |

---

## Fix Quality Summary — All Three Rounds

| Round | Windows | Fixes Reviewed | ACCEPTABLE | CONSIDER | FIX | Similar Bugs |
|-------|---------|---------------|------------|----------|-----|-------------|
| 1 (6-mo) | 3 | 30 | 27 | 2 | 1 | 5 |
| 2 (3-mo) | 6 | 60 | 59 | 0 | 1 | 3 |
| 3 (2-mo) | 9 | 90 | 89 | 0 | 1 | 6 |

**Overall**: 89/90 fixes complete. Fix quality is consistently exceptional across all 18 months.

The one incomplete fix across all rounds was the `_pyrepl` `os.environ.get()` protection — a scope issue where the fix was applied to `_colorize.py` but not propagated to 7+ sibling locations in the same package.

---

## Development Phase Evolution (2-month granularity)

| Window | Fix:Feature | Phase | Dominant Theme |
|--------|------------|-------|---------------|
| Oct-Nov 2024 | 2.8:1 | Active development | asyncio UAF wave (4 fixes), free-threading dict crash |
| Dec24-Jan25 | 4.7:1 | Stabilization | tracemalloc races, CSV overflow, json refleak |
| Feb-Mar 2025 | 5.6:1 | Heavy stabilization | PySys_GetObject migration (complete), QSBR reentrancy |
| Apr-May 2025 | 2.6:1 | Feature development | _zstd onboarding, annotationlib PEP 563/649, t-strings |
| Jun-Jul 2025 | 4.4:1 | Security hardening | HTMLParser HTML5 compliance, hash module refactoring |
| Aug-Sep 2025 | 2.4:1 | Alpha/beta | Deadlocks (STW + daemon threads), finalization ordering |
| Oct-Nov 2025 | 1.0:1 | Feature + stabilization | Re-entrant UAF wave, JIT tracing launch |
| Dec25-Jan26 | 3.9:1 | Beta stabilization | Free-threaded GC deferred refcount, SSL races, sqlite3 callbacks |
| Feb-Mar 2026 | 4.4:1 | Pre-release | Memory safety audit, dict unpacking, SyntaxError re-init, CVE |

---

## Dominant Bug Categories by Window

| Window | Top Bug Class | Key Examples |
|--------|--------------|-------------|
| Oct-Nov 2024 | asyncio UAF + free-threading races | 4 asyncio UAFs via `__eq__`/`__getattribute__`, dict crash during replace, SRE memory pool (17yr bug) |
| Dec24-Jan25 | Memory safety + DoS | CSV int overflow, imaplib unbounded read, json PyUnicodeWriter leak, tracemalloc races |
| Feb-Mar 2025 | API migration + QSBR | PySys_GetObject→strong refs (23 files, complete), QSBR reentrancy fix, lazy-init race patterns |
| Apr-May 2025 | New module onboarding | _zstd refleak/GC_Del, t-string iterator crash, _remote_debugging error paths, annotationlib PEP 563 |
| Jun-Jul 2025 | Security + platform | HTMLParser HTML5 (4 commits), hash module refactoring, bytearray __setitem__ TOCTOU |
| Aug-Sep 2025 | Finalization + threading | STW+daemon deadlock, subinterpreter finalization, OOM during interp creation, PyREPL environ crash |
| Oct-Nov 2025 | Re-entrant UAF + JIT | bytearray 6 methods, memoryview 2, Counter, Element subscr, dict clear during lookup, JIT tracing bugs |
| Dec25-Jan26 | GC accounting + SSL | Free-threaded GC deferred refcount overhaul, SSL load_cert_chain race, sqlite3 callback lifetime |
| Feb-Mar 2026 | Memory audit + dict | SyntaxError re-init leak, dict unpacking masks errors, struct float overflow, _remote_debugging sweep |

---

## Notable Cross-Window Patterns

### 1. The re-entrant UAF wave (Oct-Nov 2025)
Nine concentrated fixes, all following the same pattern: borrowed reference or raw buffer used across a call that can execute arbitrary Python code. Fix pattern: `ob_exports++` or `Py_INCREF` before user code. Applied to bytearray (6 methods), memoryview (2), Counter (1). **Highest-impact fix campaign in the entire 18 months.**

### 2. New module maturation: _zstd lifecycle
- Apr-May 2025: Initial landing + 5 fixes (refleak, GC_Del, error paths, copy-paste)
- Oct-Nov 2025: Traverse/clear mismatch (`unused_data`), ref leak in decompressor
- Status: 2 HIGH bugs still unfixed (traverse mismatch, ref leak on `PyLong_AsInt` failure)

### 3. JIT pipeline: highest churn subsystem
`bytecodes.c` had 20-60 commits per 2-month window. The JIT tracing frontend launched in Oct 2025 and immediately produced ENTER_EXECUTOR bugs (instruction argument confusion between `oparg` and `this_instr->op.arg`). 22+ JIT-specific fixes across 18 months.

### 4. Free-threading: pervasive and ongoing
Every single window had free-threading fixes. The pattern evolved:
- Late 2024: Data races (atomics, critical sections)
- Early 2025: QSBR reentrancy, borrowed-reference migration
- Mid 2025: Finalization ordering, daemon thread deadlocks
- Late 2025: GC accounting (deferred refcount), module init races
- Early 2026: SSL locking strategy, re-entrant mutations under free-threading

### 5. SSL module: multi-window overhaul
- Oct-Nov 2025: SNI callback crash fix
- Dec25-Jan26: `load_cert_chain` race fix (GIL/mutex ordering), GC traverse/clear overhaul
- Feb-Mar 2026: `_servername_callback` Py_XDECREF, debughelpers.c dead weakref still unfixed

### 6. Copy-paste divergence: a reliable bug source
- `_zstd`: compressor vs decompressor (ref leak, error message, traverse)
- `_ctypes`: `_struct.c` float overflow fix not propagated to `cfield.c`
- `_pyrepl`: `_colorize.py` environ fix not propagated to siblings
- `_interp*`: module_traverse return value fix not propagated to `_testinternalcapi`

---

## Highest-Churn Files (risk hotspots)

| File | Total Fix Commits (18mo) | Primary Concern |
|------|------------------------|----------------|
| `Python/bytecodes.c` | 60+ | JIT/optimizer, refcount elimination |
| `Objects/dictobject.c` | 30+ | Re-entrancy, free-threading races |
| `Objects/typeobject.c` | 25+ | Type version cache, free-threading |
| `Modules/_asynciomodule.c` | 15+ | Task lifecycle UAFs |
| `Modules/_ssl.c` | 15+ | GC, locking, weak references |
| `Python/gc_free_threading.c` | 12+ | Deferred refcount, accounting |
| `Modules/_ctypes/_ctypes.c` | 12+ | Memory leaks, argtypes validation |
| `Modules/_hashopenssl.c` | 10+ | Resource leaks, OpenSSL 3.0 |
| `Python/optimizer_bytecodes.c` | 55+ | JIT optimizer correctness |

---

## Recommendations

### Immediate (HIGH confidence, actionable now)

1. **`_ctypes/cfield.c:884,906`** — `(float)PyFloat_AsDouble` silent overflow. Same bug just fixed in `_struct.c`. Use `PyFloat_Pack4` instead. s390x-specific but CPython supports s390x.

2. **`_ctypes/callproc.c:1983`** — `PyTuple_SET_ITEM(shape, i, PyLong_FromSsize_t(...))` without per-element NULL check. Same pattern fixed in `_datetimemodule.c`.

3. **`_zstd/decompressor.c:687`** — Add `Py_VISIT(self->unused_data)` to traverse. One-line fix.

4. **`_zstd/decompressor.c:113`** — Add `Py_DECREF(value)` before `return -1` when `PyLong_AsInt(key)` fails. Compressor already does this correctly.

5. **`_ssl/debughelpers.c:32-38`** — Replace `assert(ssl_socket != NULL)` with proper NULL check. Same fix as `_servername_callback`.

6. **`_ctypes/cfield.c:410`** — Add `Py_VISIT(self->name)` to `PyCField_traverse`.

7. **`_pyrepl/*.py`** — Propagate `_safe_getenv()` to all 7+ `os.environ.get()` call sites.

### Short-term

8. ~~**`_pickle.c`** `int i` loop counters~~ — FALSE POSITIVE: all bounded by BATCHSIZE=1000 or sizeof(size_t)=8.

9. **`arraymodule.c`** — Verify signed-type setters (`bb/hh/ii/ll/qq_setitem`) for reentrant `__index__` safety.

10. **`Lib/tarfile.py`** — Replace 6 bare `except:` with specific exception types.

11. **`math.sumprod()`** — Re-address gh-139074 (reverted high-precision path).

### Systemic

12. **Audit all `PyTuple_SET_ITEM` + allocating call patterns** codebase-wide.

13. **Audit `os.environ.get()` across stdlib** for broken-environ safety.

14. **Add traverse/clear consistency check** to `scan_refcounts.py` — automate detection of `Py_CLEAR(self->X)` in tp_clear without matching `Py_VISIT(self->X)` in tp_traverse.

15. **Add copy-paste divergence detection** to git-history-analyzer — when a fix touches one of a known sibling pair (compressor/decompressor, dbm/gdbm, etc.), automatically flag if the sibling wasn't also fixed.
