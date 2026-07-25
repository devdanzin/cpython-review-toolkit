# error-path-analyzer — Modules/ sample (informed)

Covers **two** scanners in one pass: `scan_error_paths.py` and `scan_pyerr_clear.py`
(cpython-review-toolkit v0.8.0).

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0, full clone).
**Build used for every repro:** `/home/danzin/projects/python_build_matrix/builds/debug-gil-nojit/python`
and `.../release-gil-nojit/python`, both at `a1d580430c8`.

> **Revision divergence, stated not reconciled.** `a1d580430c8` is *newer* than the
> nominal target `4f3be1b5777` (2026-07-18 vs 2026-07-15; `4f3be1b5777` is an
> ancestor). Per-file `git diff a1d580430c8 4f3be1b5777`:
> * `Modules/_zoneinfo.c` — **identical**. Findings 1 and 2 hold verbatim at both revisions.
> * `Modules/itertoolsmodule.c` — differs only in `count_repr` (the TSAN-0006 fix,
>   unrelated). `islice_new`'s four clears are at lines **1613 / 1624 / 1629 / 1648
>   at both revisions** (verified by `git show`).
> * `Modules/_struct.c` — differs: the newer revision adds a str cache key. The flagged
>   clear survives, at **`:2667` at `4f3be1b5777`** and **`:2683` at `a1d580430c8`**.
>   Finding 3 was reproduced on the newer build and the mechanism is unchanged.
> * `_pickle.c`, `_csv.c`, `_collectionsmodule.c`, `_queuemodule.c`, `_randommodule.c`
>   — identical.

---

## Scanner volume

### `scan_error_paths.py`

| rule | sample raw | confirmed | dismissed | precision |
|---|---|---|---|---|
| `unconditional_pyerr_clear` | 3 | **1** | 2 | **33%** |
| `alloc_null_no_memerror` | 2 | 0 | 2 | **0%** |
| `missing_null_check` | 0 | — | — | n/a (denominator non-zero, see below) |
| `unchecked_return` | 0 | — | — | n/a (suppressed by design) |
| `unchecked_parse` | 0 | — | — | n/a (Argument Clinic) |
| **total** | **5** | **1** | **4** | **20%** |

### `scan_pyerr_clear.py`

| rule | sample raw | confirmed | dismissed | precision |
|---|---|---|---|---|
| `pyerr_clear_in_dealloc` | 1 | **1** (= OOM-0039, catalogued) | 0 | **100%** |
| `pyerr_clear_unfiltered_after_python_call` | 1 | **1** | 0 | **100%** |
| `pyerr_clear_on_success_path` | 0 | — | — | n/a |
| **total** | **2** | **2** | **0** | **100%** |

**Denominators checked** (a zero over a zero denominator is silence, not safety):

* `PyErr_Clear()` call sites in the 12 sample files: **16**. Five flagged (3 + 2), eight
  correctly suppressed, **three missed** — see *Recall gaps*.
* Raw-allocator sites (`PyMem_Malloc/Calloc/Realloc/RawMalloc/New/Resize`,
  `PyObject_Malloc/Calloc/Realloc`) in the 12 files: **35**
  (`_zoneinfo.c` 11, `_elementtree.c` 5, `itertoolsmodule.c` 5, `_pickle.c` 4,
  `_datetimemodule.c` 2, `_csv.c` 2, `_randommodule.c` 2, `_queuemodule.c` 2,
  `_struct.c` 1, `_collectionsmodule.c` 1, `_json.c` 0, `_heapqmodule.c` 0).
  Two flagged. I hand-read every raw allocation in `_zoneinfo.c` (the largest bucket):
  10 of 11 carry `PyErr_NoMemory()`, the 11th is documented as deliberately silent.
* `scan_pyerr_clear` reports `destructor_functions: 545`,
  `total_pyerr_clear_calls: 190`, `total_pyerr_clear_calls_in_destructors: 7`
  over all of `Modules/` — a live denominator, not an empty one.

**Net new findings in my own remit: 5** — 4 FIX + 1 POLICY, **all four FIX reproduced**
(2 as `SIGABRT`/`SystemError`, 2 as a silently substituted or discarded exception), plus
1 catalog confirmation (OOM-0039).

**Plus 3 out-of-remit FIX-grade crashes** surfaced by the reading passes over the
RUN_CONTEXT's named targets and **independently re-reproduced by me with gdb frames** —
a heap-UAF write, a `Py_INCREF(NULL)` SIGSEGV, and a SIGFPE. See *Out-of-remit crashes*.

Neither FIX-grade `_zoneinfo.c` finding came from a scanner candidate, and none of the
three crashes came from any scanner in the run — all five came from reading, and all
belong to shapes **no scanner in this toolkit currently has a rule for**.

---

## Findings

### [FIX] `Modules/_zoneinfo.c:2314, :2324, :2334` · `get_local_timestamp` · `PyLong_AsLong` sentinel with no `PyErr_Occurred()` → `ZoneInfo.utcoffset/dst/tzname/fromutc` return NULL with nothing raised

**What breaks.** In the non-`PyDateTime_CheckExact` branch:

```c
2302:        ord = PyLong_AsLong(num);
2303:        Py_DECREF(num);
2304:        if (ord == -1 && PyErr_Occurred()) {      /* <-- guarded twin */
2305:            return -1;
2306:        }
...
2312:        hour = PyLong_AsLong(num);
2313:        Py_DECREF(num);
2314:        if (hour == -1) {                          /* <-- no PyErr_Occurred() */
2315:            return -1;
2316:        }
```

and identically for `minute` (`:2322`/`:2324`) and `second` (`:2332`/`:2334`).
`PyLong_AsLong` returns `-1` both as its error sentinel *and* as the legitimate
conversion of the integer `-1`. A `dt` whose `.hour` is `-1` therefore makes
`get_local_timestamp` return `-1` **with no exception set**; `find_ttinfo`
(`:2229`) propagates it as `NULL`, and `zoneinfo_ZoneInfo_utcoffset_impl` (`:586`),
`_dst_impl` (`:609`), `_tzname_impl` (`:633`) and `zoneinfo_fromutc` (`:660`) all
`return NULL` with a clean error state.

**Guarded twin.** `Modules/_zoneinfo.c:2304`, **ten lines above, in the same function** —
`if (ord == -1 && PyErr_Occurred())`. Same API, same sentinel, correct guard.

**How Python input reaches it.** `zoneinfo.ZoneInfo.utcoffset` is declared
`dt: object` in its Argument Clinic block — there is **no type check at all**. A plain
duck type suffices; so does a real `datetime` **subclass** that overrides `hour` as a
property (`PyDateTime_CheckExact` is false for a subclass, so the attribute path is
taken).

```python
from zoneinfo import ZoneInfo
z = ZoneInfo("UTC")

class DuckDT:
    def toordinal(self): return 739000
    hour = -1; minute = 0; second = 0

z.utcoffset(DuckDT())
```

**Reproduced.**

```
$ .../builds/debug-gil-nojit/python zi_hour.py
Fatal Python error: _Py_CheckFunctionResult: a function returned NULL without setting an exception
Python runtime state: initialized
SystemError: <built-in method utcoffset of zoneinfo.ZoneInfo object at 0x76046fd9f650> returned NULL without setting an exception

Current thread 0x00007604707c3780 [python] (most recent call first):
  File "zi_hour.py", line 14 in <module>
Aborted (core dumped)
exit=134
```

```
$ .../builds/release-gil-nojit/python zi_hour.py
utcoffset: SystemError: <built-in method utcoffset ...> returned NULL without setting an exception
dst:       SystemError: <built-in method dst ...> returned NULL without setting an exception
tzname:    SystemError: <built-in method tzname ...> returned NULL without setting an exception
subclass utcoffset: SystemError <method 'utcoffset' of 'zoneinfo.ZoneInfo' objects> returned NULL without setting an exception
subclass fromutc:   SystemError <method 'fromutc' of 'zoneinfo.ZoneInfo' objects> returned NULL without setting an exception
exit=0
```

Five distinct public entry points, three sites, `SIGABRT` on a debug build.
Build-independent: `debug-ft-nojit` also emits `Fatal Python error:
_Py_CheckFunctionResult`, and `release-ft-nojit` reproduces all five `SystemError`s —
this is not GIL-build- or debug-specific.

**Class: FIX.** Shape = *return-null-without-exception* (briefing catalog). Fix is
three characters × three lines: `if (hour == -1 && PyErr_Occurred())`.

---

### [FIX] `Modules/_zoneinfo.c:1073` · `load_data` · same shape, on the TZif ingest path

```c
1072:        Py_ssize_t cur_trans_idx = PyLong_AsSsize_t(num);
1073:        if (cur_trans_idx == -1) {
1074:            goto error;
1075:        }
```

**Guarded twin.** `Modules/_zoneinfo.c:1063`, **nine lines above** —
`if (self->trans_list_utc[i] == -1 && PyErr_Occurred())`; and again at `:1103`
for `utcoff`. Two correct siblings surround the one incorrect check.

**Reachability — honest.** `trans_idx` normally comes from
`struct.unpack(f">{timecnt}B", ...)` in `Lib/zoneinfo/_common.py`, i.e. unsigned bytes,
so a *malformed TZif file alone cannot reach this*. The C code calls
`state->_common_mod.load_data(file_obj)` through `PyObject_CallMethod`, so replacing
`zoneinfo._common.load_data` does. That is a defended surface, not an accidental one:
the same function already type-checks the result
(`PyErr_Format(PyExc_TypeError, "Invalid data result type: %R", ...)`, `:993`) and
range-checks the transition index (`PyErr_Format(PyExc_ValueError, "Invalid transition
index found while reading TZif: %zd", ...)`, `:1079`). This is one incomplete check
inside that defense.

**Reproduced.**

```
$ .../builds/debug-gil-nojit/python zi_noexc.py
about to call ZoneInfo.from_file
python: ./Modules/_zoneinfo.c:462: PyObject *zoneinfo_ZoneInfo_from_file_impl(...):
        Assertion `PyErr_Occurred()' failed.
Aborted (core dumped)
exit=134

$ .../builds/release-gil-nojit/python zi_noexc.py
raised: SystemError <built-in method from_file of type object at 0x...> returned NULL without setting an exception
```

`zoneinfo_ZoneInfo_from_file_impl:462` and `zoneinfo_new_instance:275` both carry
`assert(PyErr_Occurred());` — CPython's own stated invariant for that path, and this
input breaks it.

**Class: FIX** (lower reachability than the previous one; the assert firing is
CPython's own contract being violated).

---

### [FIX] `Modules/itertoolsmodule.c:1613, :1624, :1629, :1648` · `islice_new` · unfiltered `PyErr_Clear()` after `PyNumber_AsSsize_t` replaces the user's exception with a fixed `ValueError`

`:1648` is the `scan_pyerr_clear` candidate; the other three are the siblings the
scanner missed and are the same bug.

```c
1646:            step = PyNumber_AsSsize_t(a3, PyExc_OverflowError);
1647:        if (step == -1 && PyErr_Occurred())
1648:            PyErr_Clear();
1649:    }
1650:    if (step<1) {
1651:        PyErr_SetString(PyExc_ValueError,
1652:           "Step for islice() must be a positive integer or None.");
```

`PyNumber_AsSsize_t` calls `__index__` — arbitrary Python. The unnarrowed clear
discards whatever it raised (and, being `PyErr_Clear`, drops the context so there is
no `__context__` chain either), and a fixed `ValueError` is substituted.

**Guarded twin — in the same file.** `itertoolsmodule.c:220` (`batched`, `null_item`)
and `:1993` (`chain`) both do
`if (!PyErr_ExceptionMatches(PyExc_StopIteration)) { ...return NULL; } PyErr_Clear();`.
House rule: 2 of the 6 non-trivial clears narrow, `islice_new`'s 4 do not.
The canonical fix for *this exact API pairing* is
`Objects/bytesobject.c:2936-2940` and `Objects/bytearrayobject.c:994-998`:

```c
size = PyNumber_AsSsize_t(x, PyExc_OverflowError);
if (size == -1 && PyErr_Occurred()) {
    if (!PyErr_ExceptionMatches(PyExc_TypeError))
        return NULL;
    PyErr_Clear();  /* fall through */
```

**Reproduced** (`debug-gil-nojit`, exit 0 — no crash, a silently wrong exception):

```
step  KeyboardInterrupt  -> ValueError: Step for islice() must be a positive integer or None.
start KeyboardInterrupt  -> ValueError: Indices for islice() must be None or an integer: 0 <= x <= sys.maxsize.
stop2 KeyboardInterrupt  -> ValueError: Stop argument for islice() must be None or an integer: 0 <= x <= sys.maxsize.
step  MemoryError        -> ValueError: Step for islice() ...
start MemoryError        -> ValueError: Indices for islice() ...
stop2 MemoryError        -> ValueError: Stop argument for islice() ...
step  RecursionError     -> ValueError: Step for islice() ...
start RecursionError     -> ValueError: Indices for islice() ...
stop2 RecursionError     -> ValueError: Stop argument for islice() ...
```

driven by `itertools.islice(range(10), 0, 5, Evil(KeyboardInterrupt))` with
`class Evil: def __index__(self): raise self.exc`.

**Not the "clear whose branch immediately re-raises something more specific" FP class.**
A fixed `ValueError` about the *shape* of the argument is strictly less specific than
the `KeyboardInterrupt` it replaced, and the substitution happens on the same input
that legitimately raises `TypeError`. This is the `unionobject.c:172` / CPY-0006
archetype, one API removed.

**Class: FIX** (nine reproduced substitutions across three argument positions).

---

### [FIX] `Modules/_struct.c:2667` (`:2683` at `a1d580430c8`) · `cache_struct_converter` · unfiltered `PyErr_Clear()` after `PyDict_SetItem` swallows a user `__hash__` exception

```c
2661:    s_object = PyObject_CallOneArg(state->PyStructType, fmt);
2662:    if (s_object != NULL) {
2663:        if (PyDict_GET_SIZE(state->cache) >= MAXCACHE)
2664:            PyDict_Clear(state->cache);
2665:        /* Attempt to cache the result */
2666:        if (PyDict_SetItem(state->cache, fmt, s_object) == -1)
2667:            PyErr_Clear();
```

`fmt` reaches here as a `str` **subclass**, so `PyDict_SetItem` runs the subclass's
`__hash__` — arbitrary Python. The intent ("attempt to cache") is best-effort and
correct; the *unnarrowed* clear is not, because the only exception the author expected
is a `MemoryError` from a dict resize.

**Guarded twin.** `_struct.c` has **17** `PyErr_ExceptionMatches(PyExc_OverflowError)`
narrowings (`:529, :546, :576, :595, :614, :634, :653, :667, :681, :695, :709, :727,
:1005, :1033, :1331, :1359, :2430`) and exactly **one** `PyErr_Clear()` — this one.
The house rule could not be more clearly established, and this is the sole outlier.

**Reproduced** — `struct.pack` returns successfully with the `KeyboardInterrupt` gone:

```
$ .../builds/debug-gil-nojit/python struct_hash.py
  __hash__ call 1
  __hash__ call 2
pack returned: b'\x01\x00\x00\x00'  (KeyboardInterrupt swallowed)
total __hash__ calls: 2
exit=0
```

```python
class S(str):
    n = 0
    def __hash__(self):
        S.n += 1
        if S.n >= 2: raise KeyboardInterrupt("from __hash__")
        return str.__hash__(self)
struct.pack(S("<i"), 1)
```

The first hash is consumed by `PyDict_GetItemRef` (`:2653`), the second by
`PyDict_SetItem` (`:2666`) — so a `__hash__` that is merely *stateful*, not even
hostile, loses the interrupt.

**Class: FIX.** Fix: `if (PyDict_SetItem(...) < 0) { if
(!PyErr_ExceptionMatches(PyExc_MemoryError)) { Py_DECREF(s_object); return 0; }
PyErr_Clear(); }` — keeps the best-effort caching, propagates everything else.

---

### [CONSIDER] `Modules/_zoneinfo.c:2512` · `zone_from_strong_cache` · undocumented sentinel-NULL, correct today only because both callers disambiguate

`find_in_strong_cache` (`:2432`) returns `NULL` for *both* "not in cache" and
"`PyObject_RichCompareBool` raised" — and `key` is `ZoneInfo(key)`'s `key: object`, so
a user `__eq__` runs there. `zone_from_strong_cache` forwards that ambiguous NULL under
the docstring "It returns NULL if the key is not in the cache", which does not mention
the error case.

Both callers currently disambiguate, which is why this is not a FIX:
* `eject_from_strong_cache:2470` — `else if (PyErr_Occurred()) return -1;` (the twin)
* `zoneinfo_ZoneInfo_impl:320` — `if (instance != NULL || PyErr_Occurred()) return instance;`

**Class: POLICY.** Worth one comment line on `find_in_strong_cache` /
`zone_from_strong_cache` recording that a NULL return may carry a live exception; the
next caller added is the one that breaks.

---

## New siblings of known shapes

| shape | catalogued exemplar | new sibling found here |
|---|---|---|
| `pyerr-clear-in-destructor` | OOM-0039 `deque_clear` | confirmed still present at `Modules/_collectionsmodule.c:750` — `newblock()` fails, `PyErr_Clear()`, `goto alternate_method`. Unchanged. **Hunted siblings: none.** The other 6 destructor-family clears in `Modules/` are `xibufferview_dealloc` (`_interpretersmodule.c:175/:183`, out of sample) and three in-sample non-destructor clears that all narrow correctly. |
| unfiltered clear after a Python-reaching call (CPY-0006 `unionobject.c:172`) | `PyObject_Hash` | **`_struct.c:2667`** (`PyDict_SetItem` → user `__hash__`) and **`itertoolsmodule.c:1613/1624/1629/1648`** (`PyNumber_AsSsize_t` → user `__index__`) |
| `return-null-without-exception` (gh-151968 / gh-151126 cluster) | missing `PyErr_NoMemory` | a *different* generator of the same shape: `PyLong_As*` sentinel with no `PyErr_Occurred()` — **`_zoneinfo.c:1073, :2314, :2324, :2334`** |

### Tree-wide sweep for the new generator

I swept `Modules/` + `Objects/` + `Python/` for
`x = PyLong_As*(...)` followed within 6 lines by `x == -1` with no `PyErr_Occurred` /
`PyErr_ExceptionMatches` on the guard line or the one after:

```
4 candidates
Modules/_zoneinfo.c:1072  Py_ssize_t cur_trans_idx = PyLong_AsSsize_t(num);
      GUARD: if (cur_trans_idx == -1) {
Modules/_zoneinfo.c:2312  hour = PyLong_AsLong(num);
      GUARD: if (hour == -1) {
Modules/_zoneinfo.c:2322  minute = PyLong_AsLong(num);
      GUARD: if (minute == -1) {
Modules/_zoneinfo.c:2332  second = PyLong_AsLong(num);
      GUARD: if (second == -1) {
```

**4 candidates tree-wide, 4 confirmed, 0 false positives.** The shape is entirely
confined to `_zoneinfo.c`; the rest of CPython gets it right everywhere. That is both a
tight fix boundary and a very cheap, very high-precision new scanner rule
(script: `sweep_long.py`, ~35 lines).

I ran the analogous sweep for the tri-state APIs used as a plain boolean (a `-1` error
return is **truthy**), in both forms:

* **inline**, `if (API(...))` with no sentinel comparison, over `Modules/`: **1
  candidate** — `_zoneinfo.c:1211 if (PyObject_IsTrue(tti->dstoff))`, where `dstoff` is
  an internal `PyDelta` built by `build_ttinfo`, so `timedelta.__bool__` cannot fail.
  **ACCEPTABLE, bound stated.** (`:1184`'s `tz_str != Py_None && PyObject_IsTrue(tz_str)`
  is reachable only through the same `load_data` monkeypatch as Finding 2; noted, not
  separately reported.)
* **assigned**, `rv = API(...); if (rv)`, over the 12 sample files: **1 candidate** —
  `_collectionsmodule.c:1208` `deque_contains_lock_held`. **False positive**: the body is
  `if (cmp) { return cmp; }`, so `-1` is returned through as the sentinel with its
  exception intact, and `deque_contains:1232` forwards it. Correct by construction.

**2 candidates, 0 confirmed.** This class is clean in the sample; see tuning proposal 3
for why I am *not* recommending it as a rule.

---

## Classes bounded (clean negatives, with evidence)

### `unconditional_pyerr_clear` dismissals — 2 of 3

* **`Modules/_randommodule.c:305` `random_seed` — ACCEPTABLE.**
  `if (random_seed_urandom(self) < 0) { PyErr_Clear(); /* fall back on the worst
  entropy */ if (random_seed_time_pid(self) < 0) return -1; }`. A documented,
  deliberate, *terminating* fallback: the alternate path always sets an exception if it
  also fails. FP class: *file-local status helper + deliberate retry-after-reset*.
* **`Modules/_pickle.c:2853` `write_unicode_binary` — ACCEPTABLE.**
  The failing call is `PyUnicode_AsUTF8AndSize(obj, &size)` and the dispatcher
  (`_pickle.c:4606`) reaches `save_unicode` only under `type == &PyUnicode_Type` —
  **exact** `str`, so no user code runs. The documented failure is the lone-surrogate
  `UnicodeEncodeError` (Issue #8383) and the fallback
  `PyUnicode_AsEncodedString(obj, "utf-8", "surrogatepass")` sets its own error on
  failure. Residual: a `MemoryError` from the UTF-8 cache fill is masked, but the
  fallback allocates too, so the caller still sees an exception. Not the
  arbitrary-Python class.

### `alloc_null_no_memerror` dismissals — 2 of 2

* **`Modules/_queuemodule.c:102` `resize_ringbuf` — ACCEPTABLE**, FP class
  *obligation deferred to the caller, and every caller discharges it*:
  `RingBuf_Put:164-166` calls `PyErr_NoMemory()` on failure; `RingBuf_Get:146`
  deliberately ignores it with an in-source justification ("It's safe it ignore
  allocation failures here; shrinking is an optimization that isn't required for
  correctness"). The guarded twin `RingBuf_Init:56-59` raises inline. Only two callers
  exist; both are correct. The scanner's `_callers_discharge` gate did not fire because
  the discharge lives in `RingBuf_Put`, not in `resize_ringbuf` — see tuning proposal 4.
* **`Modules/_zoneinfo.c:2357` `strong_cache_node_new` — ACCEPTABLE**, FP class
  *documented no-raise contract*. The three lines immediately above the allocation say
  so verbatim: `"This function doesn't set MemoryError if PyMem_Malloc fails, as the
  cache intentionally doesn't propagate exceptions and fails silently if error occurs."`
  Its sole caller `update_strong_cache:2547` returns `void`, so the sentinel is
  structurally unable to reach Python.

### Files with a genuinely empty denominator for both bug classes

`Modules/_json.c` and `Modules/_heapqmodule.c` contain **zero** `PyErr_Clear()` calls
and **zero** raw-allocator calls; `Modules/_datetimemodule.c` contains zero
`PyErr_Clear()` calls. Their zeros are structural, not accidental.

### `_zoneinfo.c` allocation ladder — read in full, and OOM-swept

I read all 11 raw allocations in `_zoneinfo.c`. Ten carry `PyErr_NoMemory()`
(`:1048, :1053, :1093, :1123, :1140, :1161, :1887, :1919, :2153` and the second arm of
`:1090/:1092`); the eleventh is `strong_cache_node_new`, above. `load_data`'s
`error:`/`cleanup:` ladder (`:1253-1302`) frees `trans_list_utc`, both
`trans_list_wall[]`, `_ttinfos` (with `ttinfos_allocated` as the correct partial bound),
`trans_ttinfos`, `utcoff`, `dstoff`, `isdst`, `trans_idx` — no path leaks.
`parse_tz_str`'s ladder (`:1703-1717`) frees `std_abbr`, `dst_abbr`, `start`, `end`.
Dense OOM sweep, 400 iterations, one subprocess each, on `debug-gil-nojit`:

```
payload: buf.seek(0); ZoneInfo.from_file(buf, key="x")   [real Europe/Berlin TZif]
iterations_run: 400
outcome_counts: {"memory_error": 160, "completed": 240}
crashes: []      verdict: "no crash in this range (all failures handled cleanly)"
```

Two more sweeps, same harness, `debug-gil-nojit`:
* `_queue.SimpleQueue` 64×put/64×get (drives `resize_ringbuf` grow *and* shrink),
  300 iterations — `{'memory_error': 6, 'completed': 294}`, no crash.
* `struct.Struct("<10i5s2xqd3f").pack(...)` after `struct._clearcache()`,
  300 iterations — `{'memory_error': 14, 'other_exception': 286}`, no crash.

Recorded per the briefing's caveat: **a clean OOM sweep is not an exoneration**
(gh-151815 survives 60/60 and is still live). These bound the *reproducible* risk, not
the static one.

### `PyErr_Clear()` census of the 12 sample files — all 16 sites accounted for

| disposition | count | sites |
|---|---|---|
| narrowed with `PyErr_ExceptionMatches` (correctly suppressed) | 5 | `_collectionsmodule.c:442`, `_pickle.c:1390`, `_elementtree.c:3707`, `itertoolsmodule.c:229`, `:1994` |
| after `PyLong_As*` on a `PyLong_Check`-ed operand — no user code, only `OverflowError` (correctly suppressed) | 3 | `itertoolsmodule.c:3544`, `:3569`, `:3685` |
| flagged by `scan_error_paths` | 3 | `_pickle.c:2853`, `_randommodule.c:305`, `_struct.c:2667` |
| flagged by `scan_pyerr_clear` | 2 | `_collectionsmodule.c:750`, `itertoolsmodule.c:1648` |
| **missed by both** | **3** | `itertoolsmodule.c:1613`, `:1624`, `:1629` |

No double-reporting between the two scanners: the destructor-family split held
(`deque_clear` is skipped by `scan_error_paths._DESTRUCTOR_RE`, reported by
`scan_pyerr_clear`), and no `(file, line)` pair appeared in both outputs.

---

## Out-of-remit crashes surfaced while reading — route these

I dispatched two bounded reading passes over `_pickle.c` and `_csv.c`/`_struct.c` (the
RUN_CONTEXT's named targets) for my four error-path shapes. Three of what came back are
**memory-safety** bugs, not error-path bugs — neither of my scanners has a rule that
could see them, and neither `scan_refcounts` (2 sample hits, both `itertoolsmodule.c`)
nor `scan_null_checks` (0 sample hits) flagged them either. **I re-reproduced all three
myself on the matrix builds** and captured the frames; they are recorded here so the run
owner can route them, not claimed as my scanners' output.

### [FIX] `Modules/_struct.c:2371` · `s_pack_internal` · raw `s_codes` pointer walked across a call into user `__index__` → heap-use-after-free **write**

`for (code = soself->s_codes; code->fmtdef != NULL; code++)` walks a `PyMem`-allocated
array while `e->pack(...)` (`:2429`) reaches `_PyNumber_Index` — user `__index__`. A
re-entrant `Struct.__init__` on the same object makes `prepare_s` `PyMem_Free` that array
(`:1802-1806`) and install a new one; the loop then reads `code->size` / `code->repeat` /
`code->fmtdef` out of freed memory and **calls** the resulting `formatdef *`.

Independently reproduced by me on `debug-gil-nojit` @ `a1d580430c8` (region byte-identical
to HEAD):

```
Program received signal SIGSEGV, Segmentation fault.
np_int (state=0x7ffff74b3dc0,
        p=0xddde5dddd5336d8d <error: Cannot access memory at address 0xddde5dddd5336d8d>,
        v=0x555555cc2070 <_PyRuntime+30384>, f=0x7ffff7fa6ac0 <native_table+320>)
    at ./Modules/_struct.c:624
624	    memcpy(p, &y, sizeof y);
#1  s_pack_internal (...) at ./Modules/_struct.c:2429
#2  Struct_pack_impl (self=..., values=..., values_length=10) at ./Modules/_struct.c:2480
```

`0xdd…` is the debug allocator's freed fill, and the faulting operation is a `memcpy`
**write** through a cursor advanced by a length read from the freed block. Trigger, three
lines of stdlib Python, no monkeypatching:

```python
import struct, warnings; warnings.simplefilter('ignore')
s = struct.Struct('10i')
class Evil:
    def __index__(self):
        s.__init__('2000q'); return 0
s.pack(Evil(), *([0]*9))
```

On `release-gil-nojit` the same input does **not** crash — it returns a **16000-byte**
`bytes` from a 40-byte `Struct('10i')`. Silent wrong-size output is the worse outcome.

**Route to:** `refcount-auditor` (borrowed-pointer-across-call, the crown-jewel shape
expressed over a `PyMem` block rather than a `PyObject*`) — and note that
`scan_refcounts` cannot currently see a raw `formatcode *` cursor.

### [FIX] `Modules/_pickle.c:3501` · `batch_dict_exact_impl` · unchecked `PyDict_Next` → `Py_INCREF(NULL)`

```c
3500:        if (dict_size - total == 1) {
3501:            PyDict_Next(obj, &ppos, &key, &value);   /* return value discarded */
3502:            Py_INCREF(key);
```

`_PyDict_Next` returns 0 without writing `*pkey`/`*pvalue`; `key`/`value` are NULL at that
point. `save()` at `:3504`/`:3507` runs a user `__reduce__` that can mutate the dict, so
`total` falls behind the once-captured `dict_size` while `ppos` is already past
`dk_nentries`. **Guarded twin twenty lines below: `:3521`,
`while (PyDict_Next(obj, &ppos, &key, &value))`** — the only unchecked one in the file.

Independently reproduced by me, `debug-gil-nojit` **and** `release-gil-nojit`, exit 139:

```
Program received signal SIGSEGV, Segmentation fault.
batch_dict_exact_impl (state=..., self=..., obj=0x7ffff746a750) at ./Modules/_pickle.c:3502
3502	            Py_INCREF(key);
#1  batch_dict_exact  (...) at ./Modules/_pickle.c:3561
#2  save_dict         (...) at ./Modules/_pickle.c:3602
#3  save              (...) at ./Modules/_pickle.c:4617
#4  dump              (...) at ./Modules/_pickle.c:4817
#5  _pickle_dumps_impl(...) at ./Modules/_pickle.c:8024
```

```python
import _pickle
class Evil:
    def __reduce__(self):
        d.clear(); d['c'] = 1; d['e'] = 4
        return (int, ())
d = {'a':1, 'b':2, 'c':Evil(), 'e':4}
del d['a']; del d['b']
_pickle.dumps(d, 2)
```

**Route to:** `null-safety-scanner` (unchecked fallible result dereferenced) — the shape
is `scan_null_checks`'s exactly, and it returned 0 on this sample.

### [FIX] `Modules/_struct.c:2249, :2274, :2278` · `unpackiter_*` · construction-time invariants invalidated by a later `Struct.__init__`

`Struct.iter_unpack` validates `s_size != 0` and `buf.len % s_size == 0` at construction,
then keeps only the `Struct`. Re-initializing the `Struct` afterwards breaks both.
Independently reproduced by me on the matrix builds:

| site | trigger | `debug-gil-nojit` | `release-gil-nojit` |
|---|---|---|---|
| `:2249` `unpackiter_len` — `/ self->so->s_size` | `s=Struct('i'); it=s.iter_unpack(b'\0'*8); s.__init__(''); it.__length_hint__()` | **exit 136 (SIGFPE)** | **exit 136 (SIGFPE)** |
| `:2274` `unpackiter_iternext` — bound is only an `assert` | `s=Struct('i'); it=s.iter_unpack(b'\0'*4); s.__init__('1000q'); next(it)` | `Assertion 'self->index + self->so->s_size <= self->buf.len' failed` (exit 134) | returns a **1000-tuple** — an 8000-byte read from a 4-byte buffer, exit 0 |
| `:2278` `self->index += self->so->s_size` | `s_size == 0` → zero stride | infinite iterator | infinite iterator |

The `:2249` `SIGFPE` fires on a **release** build too, so this is not a debug-only assert.
**Guarded twin:** the `self->so == NULL` exhaustion check immediately above at `:2245-2247`
— the re-init case was simply not considered.

**Route to:** `init-bypass-checker` / `refcount-auditor` (cached-invariant-invalidated-by-
reentrancy). Note the family resemblance to the `s_pack_internal` UAF: both are
`prepare_s` re-publishing state under a live consumer.

### Reported by the reading pass, **not** independently re-verified in this session

Listed for the run owner with that caveat attached — I ran out of budget before
re-reproducing them, and I am not claiming them:

* `_pickle.c:556`/`:559` — `PDATA_PUSH`/`PDATA_APPEND` do not `Py_DECREF` on
  `Pdata_grow` failure (34 call sites; reported as a LeakSanitizer hit).
* `_pickle.c:1174` — `_Pickler_New`'s `error:` label calls `PyMem_Free(memo)` where the
  guarded twin `PyMemoTable_Del` (`:823`, used by `Pickler_clear:4957`) also frees
  `memo->mt_table`.
* `_pickle.c:7618-7621` — `UnpicklerMemoProxy.clear` leaves `memo == NULL` with
  `memo_size` non-zero on `_Unpickler_NewMemo` failure. **I tried and could not reproduce
  this**: a 60-index dense OOM sweep over `u.memo.clear(); u.memo.copy()` on
  `debug-gil-nojit` returned `{'memory_error': 4, 'completed': 56}`, no crash. Treat as
  CONSIDER pending a working trigger.
* `_pickle.c:7791-7839` — `Unpickler_set_memo` writes through `self->memo` while building
  `new_memo`, so the success path discards the writes and the error path half-applies them
  (`u.memo = {0:'A',1:'B'}` then `u.memo.copy() == {}`). A C/Python parity break.
* `_pickle.c:5859` — `load_counted_bytearray` leaks the bytearray on `PyByteArray_Resize`
  failure; guarded twin eight lines above at `:5851`.
* `_pickle.c:2556`/`:2653` — `_save_bytes_data`/`_save_bytearray_data` `return -1` on
  `size < 0` with no exception set; propagates to a `SystemError` from `dumps`. **This one
  is squarely my class** — reachable only via a `PickleBuffer` over a C exporter reporting
  `view.len < 0`, so latent, but it is exactly the `return-null-without-exception` shape
  and a `PyErr_SetString` or an `assert` is the fix.

### One place the reading pass and my own repro disagree

The `_struct.c` pass classified `:2667` `cache_struct_converter` as low-severity on the
static argument that "`fmt` is provably `str`/`bytes` by this point … so no user
`__hash__`/`__eq__` runs". **That reasoning is wrong and I disproved it empirically** — a
`str` *subclass* passes `PyUnicode_Check`, `Struct(fmt)` accepts it, and its `__hash__`
runs inside `PyDict_SetItem`. See Finding 4 above, where
`struct.pack(S("<i"), 1)` returns successfully with a `KeyboardInterrupt` discarded. This
is a useful calibration data point: "provably a concrete type" arguments must say
`CheckExact`, not `Check` — the `_pickle.c:2853` dismissal in this report survives
precisely because its dispatcher uses `type == &PyUnicode_Type`.

---

## Toolkit assessment

### Precision per rule (this sample)

| scanner | rule | raw | confirmed | precision | dominant dismissal class |
|---|---|---|---|---|---|
| `scan_error_paths` | `unconditional_pyerr_clear` | 3 | 1 | 33% | deliberate documented fallback; concrete-type call (no user code) |
| `scan_error_paths` | `alloc_null_no_memerror` | 2 | 0 | 0% | caller discharges (1); documented no-raise contract (1) |
| `scan_error_paths` | `missing_null_check` / `unchecked_return` / `unchecked_parse` | 0 | — | — | — |
| `scan_pyerr_clear` | `pyerr_clear_in_dealloc` | 1 | 1 | 100% | — |
| `scan_pyerr_clear` | `pyerr_clear_unfiltered_after_python_call` | 1 | 1 | 100% | — |
| `scan_pyerr_clear` | `pyerr_clear_on_success_path` | 0 | — | — | — |

`unconditional_pyerr_clear` at 33% is *above* the 4-20% the v0.8 notes predicted at
scale. `scan_pyerr_clear` at 100%/2 is a small but perfect sample and matches the
`Objects/` result. `alloc_null_no_memerror` at 0/2 is the weak rule here — but both
dismissals are cheap for a human and the rule is quiet (2 hits over a 35-site
denominator), so it is not costing budget.

### Recall gaps found by reading

**RG-1 — `scan_pyerr_clear._reraises_after` suppresses the true positives, systematically.**
`islice_new` has four identical clears; only `:1648` was reported. The suppressor is
`_reraises_after` (`scan_pyerr_clear.py:~876`), which walks the clear's *enclosing
block* for any `PyErr_Set*`-prefixed call and returns True. Isolated with a two-function
fixture whose bodies are semantically identical:

```
$ python scan_pyerr_clear.py fixture.c
findings: 1
  pyerr_clear_unfiltered_after_python_call 9 variant_no_reraise_in_block
```

`variant_reraise_in_same_block` — the exact `islice_new:1622-1631` shape — is dropped
only because the substitute `PyErr_SetString(ValueError, ...)` happens to live in the
same brace level. Substituting a *fixed, less specific* exception for an arbitrary user
one **is** the bug; treating it as a mitigation inverts the rule. 3 of 4 true positives
lost in one function.

**RG-1b — rule 3 drops any clear whose innermost enclosing condition is a bare
`PyErr_Occurred()` probe.** `scan_pyerr_clear.py:986-988` takes `signals[0]` and
`continue`s when it names no fallible call. For
`if (stop == -1) { if (PyErr_Occurred()) PyErr_Clear(); ... }` the innermost condition is
that probe, which yields `errstate=True, calls=[]` — so the site is dropped even with
RG-1 fixed. RG-1 and RG-1b together account for exactly the three missed `islice_new`
sites (`:1624` from RG-1, `:1613`/`:1629` from RG-1b); patching both takes the function
from 1/4 to 4/4.

**RG-2 — `scan_error_paths` counts `PyErr_Occurred` as a narrowing gate, killing the archetype shape.**
`_PYERR_CLEAR_GUARD_RE` (`scan_error_paths.py:281-284`) lists
`ExceptionMatches | GivenExceptionMatches | GetRaisedException | Fetch | **Occurred** |
SetRaisedException | Restore | GetHandledException`. `PyErr_Occurred()` answers "is
*something* pending", never "is it the exception I expected" — it is not a narrowing,
it is the failure test itself. Consequence: every
`if (x == -1 && PyErr_Occurred()) PyErr_Clear();` is silently suppressed, which is the
single most common written form of the bug in CPython. `scan_error_paths` found **0 of
9** clears in `itertoolsmodule.c`, including `:1648`, which its sibling scanner reports.

Measured by patching a copy of the scanner to drop `Occurred` from that alternation:

| | sample `unconditional_pyerr_clear` | whole `Modules/` |
|---|---|---|
| v0.8.0 as shipped | 3 | 39 |
| `Occurred` removed | **8** | **46** |

The five new sample hits are `itertoolsmodule.c:1613, :1624, :1629, :1648` (**all four
confirmed FIX**) and `itertoolsmodule.c:3685` `count_repr` (one FP —
`PyLong_AsLong` on a `PyLong_Check`-ed operand). **+4 TP / +1 FP on the sample; +7
across `Modules/`.**

**RG-3 — no rule covers `PyLong_As*` sentinel-without-`PyErr_Occurred()`.**
Both FIX-grade `_zoneinfo.c` findings — the two hardest results in this pass, one an
`SIGABRT` on five public entry points with no monkeypatching — are invisible to both
scanners. The shape is mechanical and the tree-wide FP rate measured **0/4**.

**RG-4 — three Python-reachable crashes in the sample are invisible to *every* scanner in the run.**
`_struct.c:2371` (heap-UAF write, SIGSEGV), `_pickle.c:3501` (`Py_INCREF(NULL)`, SIGSEGV
on debug *and* release) and `_struct.c:2249` (SIGFPE on debug *and* release) all sit in
the two files the RUN_CONTEXT named as the richest reading targets, and none of
`scan_error_paths`, `scan_pyerr_clear`, `scan_refcounts` (2 sample hits, both
`itertoolsmodule.c`) or `scan_null_checks` (0 sample hits) surfaced any of them. Two are
mechanically detectable and worth new rules:
* **`unchecked_dict_next`** — `PyDict_Next(...)` / `_PyDict_Next(...)` as a *statement*
  rather than a condition, followed within 3 lines by a use of its out-parameters. In
  `_pickle.c` this is 1 of 9 sites; the other 8 are loop conditions. Near-zero expected
  noise.
* **`cached-invariant-across-reentrancy`** — an iterator/view type that reads
  `self-><owner>-><field>` in `tp_iternext` / `__length_hint__` where `<field>` is
  validated only in the constructor and the owner type has a re-runnable `tp_init`. The
  `unpackiter` triad is the exemplar; `assert()` used as the bound (`_struct.c:2274`) is
  a strong marker.

The `s_pack_internal` UAF is harder (a raw `formatcode *` cursor, not a `PyObject *`) and
is genuinely `refcount-auditor` territory — but it argues for widening
`scan_refcounts`'s notion of a "borrowed" load from `PyObject*`-typed slot reads to
*any* pointer into a `PyMem`-owned block that a re-runnable `tp_init` frees.

**RG-5 — `alloc_null_no_memerror`'s caller-discharge gate is one frame too shallow.**
`resize_ringbuf` was reported although `RingBuf_Put` raises `PyErr_NoMemory()` on its
`< 0`. `_callers_discharge` appears to look for a `PyErr_*` inside the *helper*, not
inside the callers' failure branches. Two-frame lookup in the same file would have
suppressed it.

### Prompt problems

* The agent prompt still tells me `alloc_null_no_memerror` "already checks whether every
  in-file caller discharges the obligation". On this sample that gate did not fire for
  either candidate (RG-4 and the documented-comment case). The prompt should say
  *attempts to check*, or the gate should be fixed — as written it invites premature
  dismissal.
* The prompt's FP list for `alloc_null_no_memerror` names "the raw memory layer" and
  "cross-file deferral" but not **"the function's own doc-comment states it deliberately
  does not raise"**, which is exactly `_zoneinfo.c:2350-2353`. Worth adding — it is a
  recurring CPython idiom for caches.
* The prompt's Phase-4 reading list ("`Py_NewRef` on a nullable parameter",
  "undocumented sentinel-NULL", "pending-exception desync") is good and produced the
  POLICY finding, but it does not name the **`PyLong_As*` == -1 without
  `PyErr_Occurred()`** shape, which is where both FIX-grade reading findings came from.
  Add it as an explicit Phase-4 target.

### Ranked tuning proposals

**1. (highest value) Drop `Occurred` from `scan_error_paths._PYERR_CLEAR_GUARD_RE`.**
One token. Measured: sample 3 → 8 (+4 TP, +1 FP), `Modules/` 39 → 46. Recovers the
entire `if (x == -1 && PyErr_Occurred()) PyErr_Clear();` class, which is the archetype
the rule was written for. To hold the FP back, reuse `scan_pyerr_clear`'s existing
`_is_python_reaching()` classifier: it already excludes `PyLong_As*` on a checked long,
which is precisely the single new FP (`count_repr:3685`). Net expected: +4 TP, +0 FP on
this sample.

**2. Make `_reraises_after` require a *narrowing or chaining* re-raise, not any `PyErr_Set*`.**
Suppress only when the following call is `PyErr_SetFromErrno*` / `_PyErr_ChainExceptions*`
/ `PyErr_SetObject` re-raising the fetched exception, or when a
`PyErr_ExceptionMatches` precedes it. A bare `PyErr_SetString(PyExc_ValueError, ...)`
after clearing a user exception should **downgrade confidence to medium**, never
suppress. Fixture in `scratchpad/fixture.c` reproduces the inversion in two
semantically identical functions.

**2b. When the innermost enclosing condition is a *bare* errstate probe, fall through to
the next condition out.** `if (stop == -1) { if (PyErr_Occurred()) PyErr_Clear(); }` —
`_condition_signals` on `PyErr_Occurred()` yields `errstate=True, calls=[]`, so rule 3's
`if not sig["calls"]: continue` (`scan_pyerr_clear.py:986-988`) drops the site. A bare
errstate probe names no failing call because it is a *nested re-test of the same
failure* the next condition out tested; walking one level out is not the
"wrong-polarity branch attribution" FP class (that class is about an enclosing branch
testing a *different* thing, e.g. `if (module) { ... }`). Loop outward only while the
condition has `errstate and not calls`.

**Measured, 2 and 2b applied together to a patched copy** (`ep_probe/scan_pyerr_clear.py`):

| | sample | `Modules/` `unfiltered_after_python_call` | `Modules/` `on_success_path` |
|---|---|---|---|
| v0.8.0 as shipped | 2 (1 + 1) | 16 | 2 |
| 2 only | 3 | 18 | 5 |
| **2 + 2b** | **5** | **20** | **5** |

2 alone recovers `itertoolsmodule.c:1624`; 2b is required for `:1613` and `:1629`.
Together they take the sample from 2 candidates to 5, **all five confirmed** —
`pyerr_clear_unfiltered_after_python_call` precision stays at 100% while recall on the
sample's worst function goes from 1/4 to 4/4.

**3. New rule `pylong_sentinel_no_errcheck` in `scan_error_paths.py`.**
Match `<lvalue> = PyLong_As{Long,LongLong,Ssize_t,Int,Size_t,UnsignedLong*}(...)`
followed within N lines by a comparison of that lvalue against `-1` (or `(unsigned…)-1`)
whose guard line and the one after contain no `PyErr_Occurred` / `PyErr_ExceptionMatches`.
Emit `confidence: high` when the guarded branch is an error return, `medium` otherwise.
Suppress when the operand is provably `PyLong_Check`-ed *and* the surrounding function
has already established a non-negative bound. Measured on
`Modules/` + `Objects/` + `Python/`: **4 candidates, 4 true positives, 0 false
positives** — the tightest signal-to-noise of anything in this pass, and it is where
this run's only crashing bug lives. Reference implementation: `scratchpad/sweep_long.py`.
**Do not ship the tri-state mirror rule on this evidence.** I measured both forms of
`PyObject_IsTrue` / `PyObject_Not` / `PyObject_RichCompareBool` / `PySequence_Contains`
/ `PyObject_IsInstance` / `PyDict_Contains` misuse (a `-1` error return is *truthy*):
* inline — `if (API(...))` with no sentinel comparison: **1 candidate in `Modules/`**,
  `_zoneinfo.c:1211`, ACCEPTABLE (internal `PyDelta`, `__bool__` cannot fail);
* assigned — `rv = API(...); if (rv)`: **1 candidate in the 12 sample files**,
  `_collectionsmodule.c:1208` `deque_contains_lock_held`, a false positive because the
  branch is `if (cmp) return cmp;` — the `-1` is *returned through* as the sentinel with
  its exception intact.

0 confirmed out of 2. `sweep_istrue.py` / `sweep_bool3.py` are recorded so the next run
does not re-derive them, but the rule would be pure noise at this density.

**3b. New rule `unchecked_dict_next`.** `PyDict_Next` / `_PyDict_Next` / `PySet_NextEntry`
appearing as an *expression statement* (not as an `if`/`while` condition and not
assigned), with a use of one of its out-parameters within the next 3 lines. Measured on
`_pickle.c`: 1 candidate of 9 call sites, and it is a **reproduced SIGSEGV on both debug
and release** (`:3501`). The remaining 8 are loop conditions and are excluded by the rule
as written. Cheapest high-value rule available after proposal 3.

**4. Deepen `_callers_discharge` to two frames within the file.**
If every in-file caller either raises on the helper's failure or provably discards the
result (a `(void)` cast, or a `void`-returning caller), suppress. Would have removed
both `alloc_null_no_memerror` dismissals in this sample, taking that rule from 0/2 to a
clean 0/0.

**5. Recognize a doc-comment no-raise contract.**
Suppress `alloc_null_no_memerror` (to `POLICY`, not silence) when the function's leading
block comment matches `/doesn'?t set MemoryError|fails silently|does not propagate
exceptions/i`. Exactly one hit in this sample, but it is a self-documenting CPython
idiom and reclassifying beats dismissing.

**6. Extend the FP taxonomy** (`data/cpython_non_bugs.md`) with the two classes measured
here, so the next informed run does not relitigate them:
*"a deliberate documented fallback that always terminates in a raise
(`_randommodule.c:305 random_seed_urandom` → `random_seed_time_pid`)"* and
*"a `PyErr_Clear()` after a concrete-type conversion the dispatcher already type-checked
(`_pickle.c:2853`, reached only under `type == &PyUnicode_Type`)"*.

---

## Repro artifacts

All under
`/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/`:

| file | what it proves |
|---|---|
| `zi_hour.py` | Finding 1 — SIGABRT on debug, 5× `SystemError` on release |
| `zi_noexc.py` | Finding 2 — `Assertion 'PyErr_Occurred()' failed` at `_zoneinfo.c:462` |
| `islice_swallow.py` | Finding 3 — 9 exception substitutions across 3 arg positions |
| `struct_hash.py` | Finding 4 — `struct.pack` succeeds with `KeyboardInterrupt` discarded |
| `sweep_long.py` | tuning proposal 3 — tree-wide `PyLong_As*` sweep, 4/4 TP |
| `sweep_istrue.py` | tri-state inline sweep — 1 hit, 0 confirmed (do not ship) |
| `sweep_bool3.py` | tri-state assigned-form sweep — 1 hit, 0 confirmed (do not ship) |
| `ep_probe/scan_pyerr_clear.py` | proposals 2 + 2b — patched copy; sample 2 → 5, all confirmed |
| `epa/struct_uaf2.py` | out-of-remit — `_struct.c:2429` heap-UAF write, SIGSEGV + gdb frame |
| `epa/struct_fpe.py` | out-of-remit — `_struct.c:2249` SIGFPE, exit 136 on debug **and** release |
| `epa/struct_oob.py` | out-of-remit — `_struct.c:2274` assert on debug, 8000-byte OOB read on release |
| `epa/pk_f1.py` | out-of-remit — `_pickle.c:3502` `Py_INCREF(NULL)` SIGSEGV + gdb frame |
| `epa/pk_f4b.py` | negative — 60-index OOM sweep, `UnpicklerMemoProxy.clear` did **not** reproduce |
| `fixture.c` | RG-1 — `_reraises_after` inversion in two semantically identical functions |
| `ep_probe/scan_error_paths.py` | RG-2 — patched copy, `Occurred` removed; 3 → 8 / 39 → 46 |
