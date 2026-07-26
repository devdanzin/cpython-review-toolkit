# pyerr-clear-auditor — slice `obj-sequences` (Group A2, INFORMED)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** exactly the 4 files in `preflight/slice_files.txt`
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`
**Build-matrix validity:** re-verified independently — `git diff a1d580430c8 4f3be1b5777 --` over the four slice files is **empty**. Every line number below is valid on every matrix build without adjustment.

**Read first:** `preflight/AGENT_BRIEF.md` §0, `agents/include-graph-mapper.md`, `agents/git-history-context.md`, `agents/refcount-auditor.md`, `agents/error-path-analyzer.md`, `preflight/informed_briefing.md`.

**Pre-run scanner:** `scan_pyerr_clear.sample.json` — 3 findings, `destructor_functions: 10`, `total_pyerr_clear_calls: 6`, `total_pyerr_clear_calls_in_destructors: 0`, `functions_analyzed: 331`.

**Reproducers** (all `.py`, all in `reports/obj-sequences/repro/`):
`pyerrclear_buffer_typeerror_overwrite.py` (modes `table` / `contract` / `buffererror` / `clears` / `reach`),
`pyerrclear_bytearray_dealloc_clobber.py` (`clean` / `live` / `cpath`),
`pyerrclear_dealloc_pending_window.py`.

---

## Headline

**The class handed to me is real, is bigger than five sites, and its worst case needs no adversarial dunder at all.**

- The five sites destroy **every** exception class a user `__buffer__` can raise — **25 of 25** (5 sites × 5 classes), on **4 of 4** builds. The three in-slice guarded twins preserve all five — **20 of 20**, 4/4 builds.
- The replacement is **not** merely "TypeError instead of KeyboardInterrupt". `b"a" + memoryview(b"abcdef")[::2]` — mundane Python, no dunder, no subclass — really raises `BufferError: memoryview: underlying buffer is not C-contiguous`, and the five sites report `TypeError: can't concat memoryview to bytes`. The in-slice twins report the `BufferError`. **The wrong exception class and the destroyed diagnosis are reachable from ordinary code.**
- `Objects/bytesobject.c:3291`, which Group A could only reach statically, is **Python-reachable**: `io.TextIOWrapper` over a buffer whose `read1()` returns a custom exporter, 5/5 classes destroyed, 4/4 builds.
- A **sixth** instance sits in `Objects/stringlib/join.h:75`, textually compiled into **both** `bytesobject.c` and `bytearrayobject.c` — `b"".join(...)` and `bytearray(b"").join(...)`, 5/5 classes destroyed on each.

**And the destructor question the task flagged is answered, in the opposite direction from both Group A verdicts.** `bytearray_dealloc:1210-1213` (the `PyErr_SetString` + `PyErr_Print` spelling my rule cannot see) violates an invariant CPython **asserts in its own debug builds** — `Objects/object.c:3321-3330`, gh-89373, *"the tp_dealloc function must leave the current exception unchanged"*. Measured: **`Fatal Python error: _Py_Dealloc: Deallocator of type 'bytearray' cleared the current exception`, SIGABRT rc=134 on `debug-gil-nojit` and `debug-ft-nojit`**; on both release builds the caller's `TypeError` becomes `SystemError: <method 'extend' of 'bytearray' objects> returned NULL without setting an exception`. `refcount-auditor` called it ACCEPTABLE and `error-path-analyzer` called it CONSIDER-with-unknown-reachability; both were reasoning about the *trigger*, and neither measured the *consequence*.

**One correction to my own work, recorded because the mistake is instructive.** My first destructor probe (`pyerrclear_dealloc_pending_window.py`) reported `PyErr_Occurred() == NULL` at dealloc time in **8 of 8** windows, which would have refuted the whole `pyerr-clear-in-destructor` bug shape. It is an artifact of the harness: `slot_tp_finalize` (`Objects/typeobject.c:11220`) calls `_PyErr_GetRaisedException()` before running `__del__` and restores at `:11242`, so **a Python-level `__del__` can never observe a pending exception**. The negative measures `slot_tp_finalize`, not CPython. The C-level probe that replaced it produced the SIGABRT above. This is the briefing's "artifacts of the harness, not of CPython" FP class firing on my own measurement.

---

# 1. Findings

## [FIX] B1 — five sites replace a user `__buffer__` exception with a fixed `TypeError`; three in-slice twins do not

| # | site | function | Python entry point |
|---|---|---|---|
| S1 | `Objects/bytesobject.c:1551` | `_PyBytes_Concat` | `b"a" + x` |
| S2 | `Objects/bytesobject.c:3291` | `PyBytes_Concat` | C API — **and `io.TextIOWrapper`, see B1.4** |
| S3 | `Objects/bytearrayobject.c:314` | `PyByteArray_Concat` (`sq_concat`, `:2757`) | `bytearray(b"a") + x` |
| S4 | `Objects/bytearrayobject.c:357` | `bytearray_iconcat_lock_held` | `ba += x` |
| S5 | `Objects/bytearrayobject.c:664` | `bytearray_setslice` | `ba.extend(x)` (via `bytearray_extend_impl:2186`) |

All five have the identical body:

```c
    if (PyObject_GetBuffer(<user object>, &v, PyBUF_SIMPLE) != 0) {
        PyErr_Format(PyExc_TypeError, "can't concat %.100s to %.100s", ...);
        <bail>
    }
```

`PyObject_GetBuffer` dispatches `bf_getbuffer`, which for any Python class defining PEP 688 `__buffer__` is `slot_bf_getbuffer` → arbitrary Python. Its `-1` means only that *something* was raised.

**Why this is not the FP class.** The taxonomy's "clear after a sentinel-returning lookup is idiomatic" entry does not cover it: this is the documented refinement — *an unfiltered discard after a call that runs arbitrary Python is not in the idiomatic class*. And the four `unconditional_pyerr_clear` FP shapes do not apply either: the setter sits on the callee's **failure** branch in all five (verified by reading all 27 `PyObject_GetBuffer` sites in the slice, §2.3), not on a later wrong-type-result or range branch.

### B1.1 — the class table (task b, measured the way CPY-0179 was measured)

`repro/pyerrclear_buffer_typeerror_overwrite.py table`. A `__buffer__` raises each class in turn; the cell is what reaches Python.

| raised from `__buffer__` | S1 | S2 | S3 | S4 | S5 |
|---|---|---|---|---|---|
| `ZeroDivisionError` | TypeError | TypeError | TypeError | TypeError | TypeError |
| `KeyboardInterrupt` | TypeError | TypeError | TypeError | TypeError | TypeError |
| `MemoryError` | TypeError | TypeError | TypeError | TypeError | TypeError |
| `SystemExit` | TypeError | TypeError | TypeError | TypeError | TypeError |
| `RecursionError` | TypeError | TypeError | TypeError | TypeError | TypeError |

**25 of 25 destroyed.** `__context__` is `None` at every cell — `_PyErr_SetObject` chains from the *handled* exception, not the pending one, so this is total destruction, not demotion. Identical on `release-gil-nojit`, `debug-gil-nojit`, `release-ft-nojit` (`PYTHON_GIL=0`), `debug-ft-nojit` (`PYTHON_GIL=0`) — **4/4 builds, 100/100 cells**.

**The twins, same probe, same builds:**

| twin | mechanism | 5 classes |
|---|---|---|
| **TA** `bytearray___init___impl:1013-1019` | `PyObject_CheckBuffer(arg)` gate, then `PyObject_GetBuffer(..., PyBUF_FULL_RO)`, then **`return -1` — propagates verbatim, no reformat at all** | 5/5 **SURVIVE** |
| **TB** `_Py_bytes_tailmatch:696-701` | same failing call (`PyObject_GetBuffer` via `tailmatch:642`), reformat **only** under `if (PyErr_ExceptionMatches(PyExc_TypeError))` | 5/5 SURVIVE (`bytes` and `bytearray` both) |
| **TC** `bytearray___init___impl:1061-1066` | same guard, on `PyObject_GetIter` | 5/5 SURVIVE |

**20 of 20 survive, 4/4 builds.** TA is the strongest twin: it is in the same file as three of the five defects, it takes the *same* argument through the *same* API, and it propagates the user's own message unchanged.

### B1.2 — what the two twin idioms actually do differently

Two distinct correct answers exist in-slice, and the five sites use neither:

1. **Gate, then propagate (TA; also `parse_args_finds_byte:406-421` in `bytes_methods.c`).** Test `PyObject_CheckBuffer(obj)` first — a `Py_TPFLAGS`/`tp_as_buffer` test that raises nothing and runs no Python. If it fails you produce your own `TypeError` with **no exception yet in flight**; if it passes, any later `PyObject_GetBuffer` failure is by definition *not* "wrong type", so there is nothing to reformat and the exception is propagated. This is the shape the plugin brief names for the `PyObject_GetBuffer` family, and it is present twice in this slice.
2. **Narrow, then reformat (TB, TC).** Keep the nicer message but gate it on `PyErr_ExceptionMatches(PyExc_TypeError)`.

The five sites call `PyObject_GetBuffer` unconditionally and reformat unconditionally.

### B1.3 — is the replacement ever load-bearing? **Partly — and where it is, it is wrong**

`repro/... contract` and `... buffererror`, 4/4 builds:

| probe | result |
|---|---|
| `b"a" + 5` | `TypeError: can't concat int to bytes` — **load-bearing**, this is the message the test suite pins |
| `bytearray(b"a") + 5` / `ba += 5` | `TypeError: can't concat int to bytearray` — load-bearing |
| `ba.extend(5)` | `TypeError: can't extend bytearray with int` — **produced by the guarded site `:2193`, not by S5.** `PyObject_CheckBuffer(5)` is false, so `bytearray_extend_impl` never calls `bytearray_setslice` |
| `b"a" + memoryview(b"abcdef")[::2]` | `TypeError: can't concat memoryview to bytes` |
| `bytearray(b"a").extend(memoryview(b"abcdef")[::2])` | `TypeError: can't set bytearray slice from memoryview` |
| **`b"abcdef".startswith(mv)` (TB twin, same input)** | **`BufferError: memoryview: underlying buffer is not C-contiguous`** |
| **`b"abcdef".find(mv)` / `mv in b"abcdef"` (propagating siblings)** | **`BufferError: memoryview: underlying buffer is not C-contiguous`** |
| `bytearray(mv)` (TA twin, `PyBUF_FULL_RO`) | succeeds |

Three conclusions, each measured:

- **For S5 the reformat is never load-bearing for a genuine wrong type.** `:664` is only reached from `extend` after `PyObject_CheckBuffer` has already returned true, so "this object is not bytes-like" is *already excluded* at that point. The plain-wrong-type message users see comes from the narrowed `:2193`.
- **For S1/S3/S4 the fixed `TypeError` is load-bearing only for the `int`-like case**, and it is applied far beyond it.
- **The most common non-adversarial failure at all five sites is a `BufferError`, not a `TypeError`.** PEP 3118 says so explicitly — *"If the exporter cannot provide a buffer of the exact type, it MUST raise `PyExc_BufferError`"* — and the five sites convert that mandated `BufferError` into a `TypeError` whose message ("can't concat memoryview to bytes") actively misdirects: it says the *type* is wrong when the type is fine and the *layout* is wrong. The in-slice twins on the identical input give the right class and the right diagnosis.

That is the answer to "is the replacement ever load-bearing for the API contract": **the message is worth keeping for one narrow case, and the current code buys it by destroying the answer in every other case, including the most common one.**

### B1.4 — S2 is Python-reachable (upgrades Group A's "static only")

`Modules/_io/textio.c:2032` calls `PyBytes_Concat(&next_input, input_chunk)` where `input_chunk` is whatever the underlying buffer's `read1()`/`read()` returned, and `next_input` is `decoder.getstate()[0]`. Both operands are user-controlled: the in-place branch needs `next_input` to be a uniquely-referenced exact `bytes` (a custom `IncrementalDecoder.getstate()` supplies one), and `input_chunk` must pass `PyObject_GetBuffer` once at `textio.c:1999` and fail it at `bytesobject.c:3290`, which a call-counting `__buffer__` does.

`repro/... reach`, 4/4 builds:

```
REACH|textio.c:2032 -> PyBytes_Concat bytesobject.c:3291|KeyboardInterrupt |TypeError|getbuffer_calls=2|can't concat TwoFaced to bytes
REACH|... |MemoryError |TypeError| ... |SystemExit |TypeError| ... |RecursionError |TypeError| ... |ZeroDivisionError |TypeError
```

5/5 classes destroyed through a public stdlib API. (`Modules/_io/bufferedio.c:1729`, the other in-tree caller, is **not** a route: `:1721` requires `PyBytes_Check(tmp)` first, so `PyObject_GetBuffer` there cannot run Python.)

Noted in passing, out of slice: **`Modules/_io/textio.c:1999-2005` is itself another instance of the same shape** — unnarrowed `PyErr_Format(PyExc_TypeError, "underlying read1() should have returned a bytes-like object, not '%.200s'")` on the failure branch of `PyObject_GetBuffer(input_chunk, ...)`. That is the mod-io slice's file, in the CPY-0173/CPY-0179 family. Location only; not reviewed.

### B1.5 — the sixth site, doubled

`Objects/stringlib/join.h:74-80` has the identical shape, and `stringlib` is **textually included** by both `bytesobject.c:1384-1395` and `bytearrayobject.c:1223-1244`, so it is compiled into two of my four files:

```c
    if (PyObject_GetBuffer(item, &buffers[i], PyBUF_SIMPLE) != 0) {
        PyErr_Format(PyExc_TypeError,
                     "sequence item %zd: expected a bytes-like object, %.80s found", ...);
```

Measured, `release-gil-nojit`: `b"".join([b"a", Raiser])` and `bytearray(b"").join([b"a", Raiser])` — **10/10 destroyed** (5 classes × 2 types). `error-path-analyzer` located this line and deferred it; I am recording the measurement and leaving ownership with whoever reviews `Objects/stringlib/`. Per the brief's hard scope boundary this is **context, not a finding of this slice** — but a fix to the five sites that skips it leaves the same defect on `join` for both types.

### B1.6 — prior art

`gh api -X GET search/issues` (plain `gh search issues` returns nothing here):

- **gh-87774 / bpo-43608, "`bytes_concat` and Buffer cleanup", OPEN since 2021-03-24, no PR.** It quotes `bytesobject.c:1549-1554` **verbatim including the `PyErr_Format` line**, but its concern is the `va.len = -1` sentinel and whether `view->len` survives a failed `GetBuffer`. The exception clobber goes unremarked. So: the exact lines have been looked at by a reporter and the defect was not noticed. The issue is also where I found the PEP 3118 `MUST raise PyExc_BufferError` quotation used in B1.3.
- `PyBytes_Concat TypeError exception`, `__buffer__ exception replaced TypeError`, `can't concat exception clobber buffer`, `PyErr_Format overwrite PyObject_GetBuffer`, `PyErr_ExceptionMatches TypeError buffer concat` → **0 results each**.
- Nothing in `cpython-review-findings` (INDEX + `catalog/known_bugs.tsv`, 182 IDs) covers these sites; the slice's existing records are CPY-0180/0181/0182.

**No prior report of this defect.**

### B1.7 — the fix

Two forms, both already in the slice. Cheapest, matching TB/TC and touching one line each:

```c
    if (PyObject_GetBuffer(b, &vb, PyBUF_SIMPLE) != 0) {
        if (PyErr_ExceptionMatches(PyExc_TypeError)) {
            PyErr_Format(PyExc_TypeError, "can't concat %.100s to %.100s", ...);
        }
        goto done;
    }
```

Better for S1/S3, matching TA and `parse_args_finds_byte`: test `PyObject_CheckBuffer(b)` first and produce the `TypeError` there, with nothing yet in flight; then let a `GetBuffer` failure propagate untouched.

For **S5** specifically, the narrowing is not enough on its own — the reformat there is dominated by an already-passed `PyObject_CheckBuffer`, so the honest fix is to delete it and propagate.

**Constraint on any fix.** `Python/bytecodes.c` does not reimplement `bytes`/`bytearray` concatenation inline the way it does list subscript, so unlike the mapper's §a warning for `listobject.c`, a fix at these five slots does run. `_PyBytes_Concat` is however named in `bytesobject.c:1540` as *"also used by PyBytes_Concat() and the specializing interpreter"* — the specializing `_BINARY_OP_INPLACE_ADD_UNICODE`-style paths call it, so the change is visible from the fast path too, which is an argument for the fix rather than against it.

---

## [FIX] B2 — the three scanner findings, confirmed, and extended to the class table

`bytearrayobject.c:1171`, `bytearrayobject.c:1177`, `bytes_methods.c:608`. **Confirmed present; both are `error-path-analyzer`'s F1/F2 and I do not re-derive them** — that report has the reading and the guarded-twin argument. What I add is the class table, which they did not run, and which changes the severity picture for `_Py_bytes_contains`.

`repro/... clears`, 4/4 builds, 5 classes:

| probe | site | 5 classes |
|---|---|---|
| `BA(b"abc") == b"abc"`, subclass `__buffer__` raises | `:1171` | **5/5 DESTROYED — returns `False`, no exception at all** |
| `bytearray(b"abc") == Raiser()` | `:1177` | **5/5 DESTROYED — returns `False`** |
| `EvilIndexPlusBuffer() in b"hello"` | `:608` | **5/5 DESTROYED — returns `True`** |
| `EvilIndexPlusBuffer() in bytearray(b"hello")` | `:608` (same C line, both types) | **5/5 DESTROYED — returns `True`** |
| `EvilIndexOnly() in b"hello"` (no fallback buffer) | `:608` | 5/5 destroyed, replaced by `TypeError` |
| twin `b"hello".find(EvilIndexOnly())` — `parse_args_finds_byte:419` propagates | — | **5/5 SURVIVE** |
| twin `bytearray(EvilIndexOnly())` — `bytearrayobject.c:996` narrowed clear | — | **5/5 SURVIVE** |

**25/25 destroyed, 10/10 twins survive, 4/4 builds.** The severity point the earlier report understates: at four of the five probes the operation **completes and returns a normal value**. `x in b` returns `True` and `ba == x` returns `False` with a `KeyboardInterrupt` or `MemoryError` erased — there is no exception for a caller to notice. Precision of the scanner's rule on this slice: **3 candidates / 3 confirmed / 0 dismissed**.

---

## [CONSIDER] B3 — `bytearray_dealloc:1210-1213` violates an invariant CPython asserts (`PyErr_SetString` + `PyErr_Print`, the CPY-0177 spelling)

```c
1209    if (self->ob_exports > 0) {
1210        PyErr_SetString(PyExc_SystemError,
1211                        "deallocated bytearray object has exported buffers");
1212        PyErr_Print();
1213    }
```

This is the only exception-state mutation in any of the slice's 12 destructors, and **my rule cannot see it** — `_CLEAR_NAMES` is `{PyErr_Clear, _PyErr_Clear}` only.

**The rule it breaks is CPython's own, and it is enforced.** `Objects/object.c:3321-3330` (gh-89373):

```c
#ifdef Py_DEBUG
    // gh-89373: The tp_dealloc function must leave the current exception unchanged.
    if (tstate != NULL && tstate->current_exception != old_exc) {
        ...
        err = "Deallocator of type '%s' cleared the current exception";
```

**Measured** (`repro/pyerrclear_bytearray_dealloc_clobber.py cpath`). The last reference is dropped from a C error path that has already set an exception — `bytearray_extend_impl:2217-2223`, where `_getbytevalue` sets `TypeError` and the next statement is `Py_DECREF(item)`:

| build | result |
|---|---|
| `debug-gil-nojit` | **SIGABRT, rc=134** — `Fatal Python error: _Py_Dealloc: Deallocator of type 'bytearray' cleared the current exception` |
| `debug-ft-nojit` | **SIGABRT, rc=134** — identical |
| `release-gil-nojit` | rc=0, the caller's `TypeError` is gone: `SystemError: <method 'extend' of 'bytearray' objects> returned NULL without setting an exception` |
| `release-ft-nojit` | rc=0, identical |

(A plain iterator, not a generator, is required: a suspended generator frame keeps the yielded object alive, so `Py_DECREF(item)` does not deallocate and the window is missed. That subtlety is in the reproducer's docstring.)

**Reachability — I agree with both Group A agents on the trigger, and I disagree on the verdict.** I re-verified all seven hand-written `ob_exports++`/`--` brackets (`:106/108`, `:1381/1385`, `:1806/1828`, `:1930/1952`, `:2565/2567`, `:2673/2675`, `:2852/2856`) and **all seven are leak-free on every path** (`goto done` or straight-line). And `bytearray_getbuffer_lock_held:66` reaches `ob_exports++` only *after* `PyBuffer_FillInfo`, which takes a strong reference to `self` — so a leaked export also leaks the reference that keeps the object alive. **There is no pure-Python route.** The reproducer manufactures the condition with `ctypes`: `PyObject_GetBuffer` with no `PyBuffer_Release`, plus a `Py_DecRef` to cancel the reference `PyBuffer_FillInfo` took — i.e. **exactly the extension bug this branch exists to diagnose**.

**Why that still makes it a finding rather than ACCEPTABLE.** The branch's whole job is to report a broken extension. As written, on a debug build it converts a *diagnosable* condition into a **fatal abort with a message about the wrong thing** ("Deallocator cleared the current exception" instead of "exported buffers"), and on a release build it converts the caller's real exception into a `SystemError` about a NULL return. A C extension author debugging their own refcount bug is precisely the person this code is for, and precisely the person it misinforms.

**Guarded twin, 1.5k lines away and in the same protocol family:** `releasebuffer_call_python` (`Objects/typeobject.c:11417-11473`) — saves with `PyErr_GetRaisedException()` at `:11423`, reports with `PyErr_FormatUnraisable` at `:11441/11454/11462`, `assert(!PyErr_Occurred())` at `:11470`, restores at `:11472`, with a comment that states the rule: *"bf_releasebuffer may be called while an exception is already active … we simply stash away the active exception and restore it."*

**Fix, one line:** replace `PyErr_SetString(...); PyErr_Print();` with
```c
    PyErr_FormatUnraisable("deallocated bytearray object has exported buffers");
```
`PyErr_FormatUnraisable` saves and restores around the report; that is what `fileio_dealloc_warn` and `releasebuffer_call_python` both use.

**Prior art:** `bytearray dealloc exported buffers SystemError` → 3 results, none on this defect; the nearest is **gh-77894 "memoryview can set an exception in tp_clear" (closed, 2018)** — the same family in the sibling type. `tp_dealloc must leave the current exception unchanged` → gh-89373 (closed, docs only). **No report of this site.**

---

## [ACCEPTABLE] B4 — `PyBuffer_Release` at `bytearrayobject.c:2391` is not itself a defect (task c)

**I agree, in one line, and I verified the line numbers rather than relaying them:** `releasebuffer_call_python` saves the in-flight exception at `Objects/typeobject.c:11423`, **reports** any `__release_buffer__` failure through `PyErr_FormatUnraisable` at `:11454` (plus `:11441` and `:11462` for the wrapper paths), asserts `!PyErr_Occurred()` at `:11470` and restores at `:11472` — so `:2391` leaves the caller's exception state exactly as it found it, and the defect at `:2375→2391→2392` is the stale `myptr`, i.e. **CPY-0180**, which I do not re-derive.

---

# 2. Classes bounded (with denominators)

## 2.1 `PyErr_Clear` / `_PyErr_Clear` in the slice — **7 sites, all read, 3 unguarded**

Hand-enumerated by grep over all four files at full length (immune to the parse truncation in §3.1):

| site | narrowing | verdict |
|---|---|---|
| `listobject.c:1322` | `if (PyErr_ExceptionMatches(PyExc_StopIteration))` `:1321` | ACCEPTABLE |
| `bytesobject.c:2940` | `if (!PyErr_ExceptionMatches(PyExc_TypeError)) return NULL;` `:2938` | ACCEPTABLE |
| `bytearrayobject.c:998` | same guard-clause form `:996` | ACCEPTABLE |
| `bytearrayobject.c:1082` | `if (!PyErr_ExceptionMatches(PyExc_StopIteration)) goto error;` `:1080` | ACCEPTABLE |
| **`bytearrayobject.c:1171`** | none | **FIX (B2)** |
| **`bytearrayobject.c:1177`** | none | **FIX (B2)** |
| **`bytes_methods.c:608`** | none | **FIX (B2)** |

`_PyErr_Clear(tstate)`: **0 occurrences** in the slice. 4 narrowed : 3 unguarded. **Scanner precision 3/3; recall 3 of 3 unguarded — but over 6 of the 7 clears it could see (§3.1).**

## 2.2 Destructor-family clears — **0 of 12 registered destructors clear, 1 of 12 mutates exception state by another spelling**

The scanner's `destructor_functions: 10` is **not** the population. The population is the set of functions actually installed in a `tp_dealloc` / `tp_clear` / `tp_finalize` / `tp_traverse` slot in the slice's type tables — **12**:

| # | slot site | function | exception-state mutation |
|---|---|---|---|
| 1 | `listobject.c:3931` | `list_dealloc` (`:554-578`) | none |
| 2 | `listobject.c:3950` | `list_traverse` (`:3426-3435`) | none |
| 3 | `listobject.c:3951` | **`list_clear_slot`** (`:908-914`) | none — **missed by the scanner** |
| 4 | `listobject.c:4000` | `listiter_dealloc` (`:4048-4056`) | none |
| 5 | `listobject.c:4017` | `listiter_traverse` (`:4058-4063`) | none |
| 6 | `listobject.c:4154` | `listreviter_dealloc` (`:4203-4210`) | none |
| 7 | `listobject.c:4171` | `listreviter_traverse` (`:4212-4217`) | none |
| 8 | `bytesobject.c:3519` | **`striter_dealloc`** (`:3417-3423`) | none — **invisible, §3.1** |
| 9 | `bytesobject.c:3536` | **`striter_traverse`** (`:3425-3430`) | none — **invisible, §3.1** |
| 10 | `bytearrayobject.c:2906` | **`bytearray_dealloc`** (`:1206-1217`) | **`PyErr_SetString` + `PyErr_Print` — B3** |
| 11 | `bytearrayobject.c:3082` | `bytearrayiter_dealloc` (`:2955-2962`) | none |
| 12 | `bytearrayobject.c:3099` | `bytearrayiter_traverse` (`:2964-2970`) | none |

`PyBytes_Type.tp_dealloc` is `0` (`bytesobject.c:3233`), so `bytes` itself contributes no destructor.

**The scanner examined 9 of these 12 (75%) and additionally counted one non-slot helper** — `list_clear` (`:902-906`), a two-line wrapper around `list_clear_impl` called from ordinary code, matched purely on its `_clear` suffix. Root cause in §3.2.

I also swept all 12 by hand for the other exception-mutating spellings — `PyErr_SetString`, `PyErr_SetObject`, `PyErr_Format`, `PyErr_Print`, `PyErr_WriteUnraisable`, `PyErr_FormatUnraisable`, and dropped fallible results. **One hit: `bytearray_dealloc`.** The other eleven contain only `Py_XDECREF` / `Py_VISIT` / `PyObject_GC_Del` / freelist calls.

**Honest bound on the negative:** `Py_XDECREF` in a destructor can run a user `__del__`, which is a Python-level finalizer and therefore *does* have its own save/restore (`slot_tp_finalize:11220/11242`). So "no exception-state mutation" for the other eleven is a statement about their own code, not about everything reachable from them.

## 2.3 The `PyObject_GetBuffer` failure branch — **27 calls, all classified; 7 replace, 2 clear, 18 propagate**

Independent sweep of every `PyObject_GetBuffer` call in the four files (`scratchpad/sweep_getbuffer_branches.py`, prints the 8 lines after each), then hand-read:

| disposition of the failure branch | calls | distinct sites | where |
|---|---|---|---|
| **replaces with a fixed `TypeError`** | **7** | **5** | `bytesobject.c:1549`+`:1550` (one `||`, one `PyErr_Format` at `:1551`), `:3290`; `bytearrayobject.c:312`+`:313` (one `||`, `PyErr_Format` at `:314`), `:356`, `:663` |
| clears unnarrowed | 2 | 2 | `bytearrayobject.c:1170`, `:1176` (B2) |
| propagates verbatim | 18 | 18 | `bytesobject.c:1879, 1967, 2107, 2290, 2309, 2665, 2966`; `bytearrayobject.c:139, 1018, 1657, 1670, 1819, 1943, 2370`; `bytes_methods.c:469, 578, 609, 642` |

Worth naming because it is the pattern a rule must not mistake for a defect: **`bytes_methods.c` narrows one frame up.** `tailmatch:642` *propagates* its `GetBuffer` failure as `-1`, and the narrowing + reformat live in its caller `_Py_bytes_tailmatch:696-701`. A rule that only inspects the 8 lines after a `GetBuffer` sees "propagate" and is right; a rule that only inspects the reformat sees a narrowed one and is right. Neither sees the whole design.

**Precision of the naive heuristic, measured.** "A `PyErr_Set*`/`PyErr_Format` within 8 lines of a `PyObject_GetBuffer`" flags **9** branch sites across the slice, of which **5** are true positives — **56%**. The 4 false positives are the shape `error-path-analyzer` describes: `bytesobject.c:2290`→`:2297` and `bytearrayobject.c:1657`→`:1661` (the two `translate` table-length checks), `bytesobject.c:2665`→`:2672` (the `fromhex` `else` arm), `bytes_methods.c:609`→`:616` (a `byte must be in range(0, 256)` check). All four are reached **only after the call succeeded**. Adding their rule-3 gate ("suppress when the setter is not on the callee's failure branch") takes it to **5/5**. **My sweep recovers their 5 true positives and no more — an independent confirmation of both the count and the discriminator.**

## 2.4 What I did not do

- **No parity oracle.** Per the brief these types have no pure-Python twin, so the CPY-0179 differential (does the dunder run at all?) is unavailable. My substitute is the **in-slice C twin under identical input** — TB/TA on the same `memoryview` and the same `Raiser` — which is stronger for this class than a backend comparison would have been, because both arms are in the same binary.
- **No OOM injection.** The classes here are destroyed regardless of allocator state; `set_nomemory` would add nothing.
- **`Objects/stringlib/join.h`** is measured but not reviewed — out-of-scope file.
- **`Modules/_io/textio.c:1999`** is located, not reviewed — different slice.
- **B3's release-build `SystemError`** is measured; I did not check whether any real extension in the wild triggers it.
- **CPY-0180 / CPY-0181 / CPY-0182 / gh-153570 / gh-153578** — confirmed present at the ref by the Group A reports; not re-derived.

---

# 3. Toolkit assessment

## 3.1 [CRITICAL, second instance] The `bytesobject.c` parse truncation costs *this* rule 2 destructors and 1 clear

`refcount-auditor` §6.1 established that `tree_sitter_utils.extract_functions` stops at line **2784** of `Objects/bytesobject.c` (3,925 lines), because the `bytes_methods[]` `PyMethodDef` table at `:2787-2843` interleaves brace initialisers with bare Argument-Clinic macro names. I confirmed it independently (`scratchpad/enum_destructors.py`: 67 functions, last ending at 2784) and can now quantify it **for this rule specifically**:

| quantity | scanner | truth | gap |
|---|---|---|---|
| `functions_analyzed` | **331** | **371** (error-path-analyzer's column-0 ground truth) | 40, **all** in `bytesobject.c` (67 vs 107) |
| `total_pyerr_clear_calls` | **6** | **7** (raw grep, all four files) | `bytesobject.c:2940` |
| destructor-family functions | **10** | **12** registered slots | `striter_dealloc`, `striter_traverse` (truncation) + `list_clear_slot` (§3.2); one spurious (`list_clear`) |

The missed clear at `bytesobject.c:2940` happens to be **correctly narrowed**, so no finding was lost — **by luck, not by coverage**. A `PyErr_Clear()` anywhere in the 1,141 invisible lines would have been silently absent from both the numerator and the denominator.

**Proposal (seconding refcount-auditor 6.1, with a rule-specific addition that is 5 lines).** The envelope already carries `total_pyerr_clear_calls`. Cross-check it against a raw byte-level count of `PyErr_Clear(` + `_PyErr_Clear(` per file and, on disagreement, emit a `notes[]` entry:

> `"Objects/bytesobject.c: 1 PyErr_Clear call(s) present in the file were not attributed to any function (tree-sitter parsed 67 of ~107 functions); results for this file are partial."`

This rule is unusually well-placed to carry that canary because its numerator is a *countable token*, not an inferred structure — the disagreement is mechanical. It would have flagged 6 ≠ 7 automatically, on this slice, with no analysis. `denominators` should carry `total_pyerr_clear_calls` too; today it holds only `files_analyzed`, `functions_analyzed`, `findings`, `destructor_functions`, so the one number that exposes the gap is not where a reader checks denominators.

## 3.2 [NEW] `_collect_destructor_slot_map` resolves nothing on a positional `PyTypeObject` — and that is the *same* defect the mapper found in `scan_recursion_guards`

`_SLOT_DESIGNATED_RE` matches `.tp_dealloc = X` and `_SLOT_SPEC_RE` matches `{Py_tp_dealloc, X}`. **All four slice files use positional initializers with comment markers:**

```c
    list_clear_slot,                            /* tp_clear */
```

Measured: `slot_map == {}` for **all four files**. Classification therefore falls back 100% to `_DESTRUCTOR_SUFFIXES` name matching, with two measured consequences in one file:

- **False negative:** `list_clear_slot` — the *actual* `tp_clear` (`listobject.c:3951`) — ends in `_slot`, not `_clear`, and is never classified.
- **False positive:** `list_clear` (`:902-906`) — a plain helper called from `list_dealloc`-unrelated paths — *is* classified as `tp_clear` on its suffix.

Both happen to be harmless here (neither clears), but the rule's severity weighting (`high` for dealloc/finalize/clear vs `medium` for traverse) and its entire denominator ride on this classifier.

**This is the include-graph-mapper's finding, in a second rule.** They reported `scan_recursion_guards` returning `slot_classification: {from_slot_map: 0, from_name_suffix: 13}` on this same slice, for the same reason — `PyList_Type` is a mixed positional/designated table. **Two rules, one root cause.**

**Proposal.** Add a shared `tree_sitter_utils` helper that parses a positional `PyTypeObject` initializer by walking its `initializer_list` children in order and reading the trailing `/* tp_xxx */` comment on each — CPython writes that comment on essentially every positional type table, and the comment is *adjacent to the value*, so this is not the taxonomy's "markers only exist in comments" trap (the marker here is a position confirmation, not the only evidence). Fall back to index-based `PyTypeObject` layout when the comment is absent. `scan_pyerr_clear` and `scan_recursion_guards` both consume it; `scan_ft_races`' `_collect_iternext_names` already does something equivalent for `tp_iternext` and can be the model.

## 3.3 [NEW, the recall gap that produced this report's main finding] The rule models `PyErr_Clear` but not `PyErr_Format`/`PyErr_SetString`

`_CLEAR_NAMES = {"PyErr_Clear", "_PyErr_Clear"}`. The five B1 sites destroy the pending exception just as completely — `_PyErr_SetObject` does not chain a merely-*pending* exception into `__context__`, measured `ctx=None` at all 25 cells — but no rule in the toolkit sees them. Same for `bytearray_dealloc`'s `PyErr_SetString` + `PyErr_Print` (B3), the CPY-0177 spelling the task flagged.

`error-path-analyzer` §3.3 proposes `unconditional_pyerr_replace` inside `scan_error_paths`. **I second the rule and dissent on its home: it belongs in `scan_pyerr_clear.py`,** because everything it needs already exists there and nowhere else:

| requirement | already in `scan_pyerr_clear` |
|---|---|
| the arbitrary-Python callee set | yes — rule 3's `failing_call` vocabulary (`PyObject_Hash`, `PyObject_GetBuffer`, `PyNumber_AsSsize_t`, `PyObject_Call*`, `bf_getbuffer`, import hooks) |
| "innermost enclosing branch tests the failure of a Python-reaching call" | yes — rule 3's gate, verbatim |
| "no `PyErr_ExceptionMatches` between the test and the site" | yes — rule 3's narrowing gate |
| destructor-family attribution and the save/restore bracket test | yes — rule 1 |

**The change is a generalisation, not a new rule:** replace the `name in _CLEAR_NAMES` membership test with a callee *class* — `{clear: PyErr_Clear/_PyErr_Clear, replace: PyErr_Set*/PyErr_Format/_PyErr_Set*/_PyErr_Format, report_and_consume: PyErr_Print}` — and add a `destroyed_by` field to the finding. Rule 1 then covers `bytearray_dealloc` (B3) and rule 3 covers the five B1 sites, with no new gates.

**Two calibration points my measurement supplies:**

1. **The precision discriminator is `error-path-analyzer`'s rule 3** ("suppress when the setter is not on the callee's failure branch"), and I confirmed it independently at §2.3: over all 27 `PyObject_GetBuffer` calls in the slice the naive within-8-lines heuristic flags 9 branch sites for 5 true positives (**56%**); with the gate it is **5/5**. That gate is not optional.
2. **Severity should be `high`, not `medium`, when the failing call is `PyObject_GetBuffer` and a `PyObject_CheckBuffer` gate has already been passed** — B1.3 measures why: at `bytearray_setslice:664` the wrong-type interpretation the fixed `TypeError` asserts is *provably excluded* before the call, so the reformat is wrong on every reachable path.

**Fixtures already in-tree, all measured here:** true positives `bytesobject.c:1551`, `:3291`, `bytearrayobject.c:314`, `:357`, `:664`; guarded twins `bytearrayobject.c:1013` (gate-then-propagate), `:1061` and `bytes_methods.c:696` (narrow-then-reformat); FP controls `bytesobject.c:2297`, `:2672`, `bytearrayobject.c:1661` (setter on a post-success branch).

## 3.4 [NEW] FP-taxonomy amendment

The entry *"an unfiltered clear after a call that runs arbitrary Python is NOT in the idiomatic class"* (briefing §"PyErr_Clear — widened-scanner FP classes") is correct and load-bearing, but it says **clear**. Everything in it applies verbatim to a **replace**. Proposed wording change: *"an unfiltered clear **or replacement** after a call that runs arbitrary Python…"*, with `Objects/bytearrayobject.c:1013` added beside `set_orig_class` as the second guarded-twin shape (gate with a raise-free type test, then propagate — the answer specific to the `PyObject_GetBuffer` family).

Second amendment, from my own error: add to *"Dynamic verification — artifacts of the harness"* —

> **A Python-level `__del__` can never observe a pending exception.** `slot_tp_finalize` (`Objects/typeobject.c:11220/11242`) brackets the call with `_PyErr_GetRaisedException` / `_PyErr_SetRaisedException`. Any probe that calls `PyErr_Occurred()` from `__del__` measures `slot_tp_finalize`, not the window under test. To exercise a C `tp_dealloc` with a live exception, drop the last reference from a C error path that has already set one (e.g. `bytearray_extend_impl:2221`, `Py_DECREF(item)` immediately after `_getbytevalue` fails) — **and use a plain iterator, not a generator**, because a suspended generator frame keeps the yielded object alive.

This cost me one wrong 8-of-8 negative and would have cost the next agent the same.

## 3.5 What worked

The informed-mode guarded-twin rule earned its keep twice. The `memoryview(b"abcdef")[::2]` differential — the strongest evidence in this report, because it needs no adversarial object — came directly from asking "what does TB do with the same input that S1 gets", not from any scanner output. And CPython's own `_Py_Dealloc` debug assertion (gh-89373) is, in effect, an upstream-maintained guarded twin for the whole `pyerr-clear-in-destructor` shape: it is worth running every destructor candidate under a debug build, because the interpreter will tell you.

---

## Summary line

```
FIX=2 (B1: 5 sites + 1 doubled sibling out-of-slice; B2: 3 sites, scanner-found)
CONSIDER=1 (B3 bytearray_dealloc:1210 -- SIGABRT on both debug builds, gh-89373 invariant)
ACCEPTABLE=1 (B4 PyBuffer_Release:2391, agreeing with Group A in one line)
POLICY=0

scan_pyerr_clear: 3 candidates / 3 confirmed / 0 dismissed -> precision 100%
  ... over 6 of the slice's 7 PyErr_Clear calls (the 7th is inside the bytesobject.c parse truncation)
  ... and 9 of the slice's 12 registered destructor slots (denominator reported as 10, incl. 1 spurious)
clears audited by hand: 7 of 7 -- 4 narrowed, 3 unguarded
destructors audited by hand: 12 of 12 -- 1 mutates exception state, by a spelling no rule models
PyObject_GetBuffer failure branches audited: 27 of 27 calls -- 7 replace (5 sites), 2 clear, 18 propagate
measurement: 4/4 builds; 25/25 classes destroyed at the 5 sites; 20/20 preserved at the 3 twins;
             25/25 destroyed at the 3 scanner sites; 10/10 preserved at their 2 twins;
             5/5 destroyed through io.TextIOWrapper (S2 reachability); 10/10 destroyed via join.h
```
