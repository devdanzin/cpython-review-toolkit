# parity-checker — Modules/ sample (informed)

**Toolkit:** cpython-review-toolkit v0.8.0 · **Target:** `/home/danzin/projects/cpython` main @ `4f3be1b5777` (3.16.0a0)
**Interpreters:** `/home/danzin/projects/python_build_matrix/builds/{debug-gil-nojit, debug-gil-nojit-asan, release-gil-nojit, release-gil-nojit-asan}/python`, all built from `a1d580430c8`.
`git diff 4f3be1b5777 a1d580430c8 -- Modules/_datetimemodule.c Lib/_pydatetime.py Modules/_zoneinfo.c Lib/zoneinfo/ Modules/_pickle.c Lib/pickle.py Modules/_json.c Lib/json/` is **empty** — every file cited below is byte-identical between the build and the review target. (`a1d580430c8` is a *descendant* of HEAD, not an ancestor; the divergence is in files I did not cite.)

## Scanner volume

`find_parity_pairs.py` is a **discovery** script, not a bug scanner, so "precision" is measured against *usable differential pairs*, not findings.

```
raw pairs: 39  (high 6, medium 12, low 21) | files_analyzed 136
usable as a real dual-backend differential:  8 of 39
false pair at the HIGHEST tier:              1 of 6 high  (`long`)
missed twin inside the review sample:        1          (`zoneinfo`)
pairs differentially tested:  datetime, zoneinfo, json, pickle, heapq, _collections
inputs executed across all pairs:  ~460,000
FIX findings (new, recorded):      2   (CPY-0032 datetime, CPY-0033 zoneinfo)
known findings confirmed:          3   (CPY-0030, gh-125318, gh-132461)
```

Adversarial inputs executed: **~360,900** — 118 hand-authored probes across 5 batches (pickle-state, tzinfo hostility, strftime, `__new__`-bypass, reduce round-trips, timestamp/ordinal extremes, hostile `__index__`/`__eq__`/`__lt__`) plus a 360,000-trial randomized differential fuzz of the three `fromisoformat` parsers. Every trial ran in its own subprocess; verdicts read from the child exit code.

**Backend selection was asserted, not assumed.** `datetime.datetime.__module__` is `'datetime'` for **both** backends and is useless as a discriminator. The working assertion, baked into every prelude:

```python
# C side
import datetime as m
assert type(m.datetime.replace).__name__ == 'method_descriptor'
# pure-Python side
import _pydatetime as m
assert type(m.datetime.replace).__name__ == 'function'
```

## Findings

### [FIX] `format_ctime` indexes `DayNames[]` with a negative weekday → OOB read, `.rodata` disclosure, SIGSEGV (`Modules/_datetimemodule.c:1713`)

Recorded as **CPY-0032** in `cpython-review-findings` (`reports/CPY-0032-datetime-pickle-state-year0-ctime-oob/`, commit `9e905c1`), status `reproduced`, `found_by: parity-checker`.

- **Input (minimized):** `datetime.date(b'\x00\x00\x01\x01').ctime()`
- **C backend** (`import datetime`): exit **139 (SIGSEGV)** on `release-gil-nojit`, deterministic **5/5**
- **C backend, ASan:** `global-buffer-overflow` READ of size 8, **8 bytes before `format_ctime.DayNames`** (56-byte array)
- **C backend, debug:** exit **134 (SIGABRT)**, `./Modules/_datetimemodule.c:474: int days_before_year(int): Assertion 'year >= 1' failed.`
- **Pure-Python twin** (`import _pydatetime`): exit **0**, returns `'Sat Jan  1 00:00:00 0000'`

```
#0 format_ctime  Modules/_datetimemodule.c:1713:33
#1 date_ctime    Modules/_datetimemodule.c:3644:12
0x... is located 8 bytes before global variable 'format_ctime.DayNames'
      defined in './Modules/_datetimemodule.c:1702' (size 56)
```

**What breaks.** `date_new` (`:3233`) and `datetime_new` (`:5485`) implement the pickle `__reduce__` backdoor: a `bytes` argument of the right length is `memcpy`'d straight into the object payload. The **only** validation is `MONTH_IS_SANE(state[2])` — the 2-byte year and the day byte are copied verbatim. `year = 0` reaches `days_before_year(0)`, which returns `-365` (its own comment says *"This is incorrect if year <= 0"*). `weekday()` then evaluates `(ymd_to_ord(...) + 6) % 7` in **C**, where `%` truncates toward zero, so `wday ∈ [-6, 0]`. `format_ctime` indexes `DayNames[wday]` with no bounds check and passes the loaded qword to `PyUnicode_FromFormat("%s")`, which dereferences it.

**Guarded twin (two, pointing at the same fix).**
1. `iso_to_ymd` in the *same file* opens with `if (iso_year < MINYEAR || iso_year > MAXYEAR) return -4;` — added by commit `d5f1139c795`, *"gh-117534: Add checking for input parameter in iso_to_ymd (#117543)"*. That closed the **other** Python-reachable entry into `days_before_year(year < 1)`.
2. `_pydatetime.date.ctime` writes `weekday = self.toordinal() % 7 or 7`. Python's `%` **floors**, so the index is never negative. That modulo-semantics difference is the entire bug — exactly the kind of C/Python seam the twin oracle exists to expose.

**How Python input reaches it.** No `ctypes`, no `_testcapi`:
```python
datetime.date(b'\x00\x00\x01\x01').ctime()                     # direct
pickle.loads(<32 attacker-controlled bytes>).ctime()           # also exit 139
```

**Information disclosure.** Walking the day byte drives `wday` over `-6..0`; each negative index returns a *different* qword of the `.rodata` preceding `DayNames` to Python as an ordinary `str`:
```
day=3  weekday()=-6  ctime()='weeks Jan  3 00:00:00 0000'
day=5  weekday()=-4  ctime()='year Jan  5 00:00:00 0000'
day=6  weekday()=-3  ctime()='week Jan  6 00:00:00 0000'
day=7  weekday()=-2  ctime()='day Jan  7 00:00:00 0000'
```
Indices landing on a non-pointer qword segfault instead.

**Maintainer intent — and therefore which fix.** `Lib/test/datetimetester.py:test_backdoor_resistance`
documents the unvalidated-field constructor as a **deliberate** trade-off: *"For fast unpickling,
the constructor accepts a pickle byte string... **This can create insane objects.** The constructor
doesn't want to burn the time to validate all fields, but does check the month field."* The same
test anticipates *"If the implementation changes to do more-careful checking, it may blow up
because other fields are insane."* So the existence of insane objects is known and intended; what
is not intended is that one of them causes a **memory-safety violation**. The primary fix is
therefore at the **crash site**, not the constructor: bound `wday` in `format_ctime`, or make
`weekday()` use a floored modulo. Tightening `date_new`/`datetime_new` with the `iso_to_ymd`
`MINYEAR`/`MAXYEAR` guard is the secondary option — it would also silence the debug assert for
every other operation, but it contradicts the documented performance decision and would likely
require updating that test.

**Class: FIX.** Memory-safe Python does not segfault; a SIGSEGV where the shipped twin returns cleanly is unambiguously a C-side defect. Not covered by any FP class in the taxonomy.

### New siblings of known shapes

**This finding *is* the sibling.** [gh-117534](https://github.com/python/cpython/issues/117534) reported the identical `days_before_year` assertion via `datetime.datetime.fromisoformat('0000W25')`; PRs #117543/#117689 fixed it **entry-point-locally** in `iso_to_ymd`. The guard never propagated to the pickle-state constructor, which is (a) still reachable and (b) strictly worse — a real OOB read and SIGSEGV on a *release* build, not merely a debug assertion. Verified on this tree: `fromisoformat('0000W25')` now raises `ValueError` while `date(b'\x00\x00\x01\x01').ctime()` segfaults. This is the informed-mode payoff: an un-found sibling of an already-fixed shape, located by asking "what *other* callers reach the guarded function?"

### [CONSIDER] `parse_hh_mm_ss_ff` / `parse_isoformat_time` accept input the twin rejects — two distinct mechanisms

Same function pair, same shape: **the "there is unconsumed input" signal is unreliable.** No memory unsafety; a validator-laxity divergence with a NUL-truncation security shape, since `datetime.fromisoformat` is a common parser for untrusted strings (JSON APIs, logs).

**(a) `'\0'` used as end-of-string instead of `p_end`.** Two sites in `parse_hh_mm_ss_ff`: `return c != '\0';` inside the field loop and `return *p != '\0';` at the end. An embedded NUL exactly at the end of the parsed time makes the parser report "clean end of string" although `p < p_end`.

```
input                                    C accelerator                          _pydatetime
'01:02:03\x00'                           time(1, 2, 3)                          ValueError
'01:02:03\x00Z'                          time(1, 2, 3, tzinfo=utc)              ValueError
'01:02:03.123456\x00<<<attacker data>>>' time(1, 2, 3, 123456)                  ValueError
'2020-01-01T00:00:00\x00'                datetime(2020, 1, 1, 0, 0)             ValueError
```
In the fractional-second path the final `*p != '\0'` never consults `p_end` at all, so **arbitrary trailing data is silently discarded**.

**(b) The time part's trailing-junk return code is dropped when a timezone follows.** `parse_isoformat_time` honors `rv == 1` only in the *no-timezone* branch (`else if (tzinfo_pos == p_end) { if (rv == 1) return -5; }`) — that branch **is the guarded twin**. When a tz is present, `rv` is overwritten by the *timezone's* parse and the time part's signal is lost:

```
'227'        -> ValueError   (guarded: no tz, rv==1 honored)
'227Z'       -> time(22, 0, tzinfo=utc)          <- '7' swallowed
'22XZ'       -> time(22, 0, tzinfo=utc)          <- 'X' swallowed
'22:00:007Z' -> time(22, 0, tzinfo=utc)
'01:02:037Z' -> time(1, 2, 3, tzinfo=utc)
```
All reach `datetime.datetime.fromisoformat` (`'2020-01-01T22XZ'` → `datetime(2020,1,1,22,0,tzinfo=utc)`).

**Class: CONSIDER.** The twins are documented as not byte-for-byte identical, so a maintainer may accept it. But this is *acceptance* divergence, not error-message divergence, and the guarded twin sits in the same function — so it reads as an oversight rather than a policy.

### [ACCEPTABLE] `OverflowError` vs `ValueError` on out-of-C-int-range integers

A consistent, module-wide class: whenever a Python int exceeds C `int`, the accelerator raises `OverflowError: Python int too large to convert to C int` where the twin raises a domain `ValueError`. Observed at `date(<__index__ returning 10**100>, 1, 1)`, `datetime(..., fold=2**63)`, `date.fromisocalendar(10**20, 1, 1)`, `date.fromordinal(10**30)`. Both reject; only the exception type differs. Squarely inside the documented non-identity. One class, not four findings.

### [ACCEPTABLE] `datetime - <timedelta subclass overriding __neg__>`

C computes `2019-12-31`; the twin propagates the subclass's `RuntimeError` because it routes through `__neg__`. Behavioural, not a defect.

### [FIX] `_zoneinfo` `get_local_timestamp` conflates the *value* `-1` with an *error* (`Modules/_zoneinfo.c:2314/2324/2334`)

Recorded as **CPY-0033** (commit `71ebbdd`), status `reproduced`, `found_by: parity-checker`.
Found only because I identified the `zoneinfo` twin **by hand** — `find_parity_pairs.py` missed it.

- **Input:** a `datetime` subclass whose `hour` (or `minute`, or `second`) property returns `-1`, passed to `ZoneInfo("UTC").utcoffset(...)`
- **C backend, debug:** exit **134**, `Fatal Python error: _Py_CheckFunctionResult: a function returned NULL without setting an exception` — 3/3 per field
- **C backend, release:** exit 1, `SystemError: <method 'utcoffset' of 'zoneinfo.ZoneInfo' objects> returned NULL without setting an exception`
- **Pure-Python twin** (`zoneinfo._zoneinfo.ZoneInfo`): exit **0**, `0:00:00`

`PyLong_AsLong` returns `-1` both for the integer `-1` and on failure, so the idiom must narrow with `PyErr_Occurred()`. Four attribute reads sit in one function; the first narrows, the next three do not.

**Guarded twin — four lines above the first defect, and proven by differential rather than by inspection:**

| field | debug exit | release outcome |
|---|---|---|
| `ord` (`toordinal() -> -1`) — **guarded** (`if (ord == -1 && PyErr_Occurred())`) | 0 | `0:00:00` |
| `hour` / `minute` / `second` (`-> -1`) | **134** each | `SystemError` each |

**Control** pins the defect to the value rather than the error path: `hour -> 2**100` raises `OverflowError` correctly.

**Class: FIX**, but **severity stated honestly** — this is *not* memory-unsafe. Release gives a clean `SystemError` caught by the interpreter's own result check; only assertions escalate it to a fatal abort. It is the briefing's `return-null-without-exception` shape with the guarded twin in the same function. Fix is three lines.

### Confirmed, not re-litigated (informed-mode rule 1)

- **CPY-0030** — `Objects/odictobject.c:1098 _odict_popkey_hash` `Py_NewRef(failobj)` with `failobj == NULL`. Still reproduces: exit **139** on debug and release, 3/3 each; twin raises `KeyError: 'dictionary is empty'`. **New reachability route worth appending to the record:** the catalogued trigger is an inconsistent `__eq__`; a *dict-level desync* reaches the same line with no hostile dunder at all — `o = OrderedDict(a=1); dict.clear(o); o.popitem()`. `dict.clear` empties the dict but leaves `od_first`/`od_last` set, so `_odict_EMPTY` is false, the node is found, `_PyDict_Pop_KnownHash` returns 0, and the unguarded branch runs. Four variants (`popitem()`, `popitem(last=False)`, `pop('a')`, and `dict.popitem(o)` as the desync) all segfault; `o.pop('a', None)` does not — consistent with the diagnosis. Also on stock `/usr/bin/python3.14`. *Scope escape — `odictobject.c` is outside the 12-file sample.*
- **gh-125318** (open) — `_zoneinfo` `utcoffset`/`dst`/`tzname` take `dt: object` with no `PyDateTime_Check`, and `find_ttinfo` applies `PyDateTime_DATE_GET_FOLD` unconditionally. Reproduced under ASan as a `heap-buffer-overflow READ of size 1`, 19 bytes past a 32-byte region. **Presented as known.** `fromutc` is safe. Note this does *not* subsume CPY-0033: a datetime *subclass* with a lying property passes any type check.
- **gh-132461** (open) — `Objects/odictobject.c:1052` `assert(_odict_find_node(self, key) == NULL)`. Debug-only SIGABRT, release fine. Reached here by dict-level desync (`dict.clear(o); o.setdefault('a')`) rather than the reported unstable-hash route — a **new trigger for a known issue**, not a new finding.

### Clean negatives from the secondary sweep

Each with an asserted backend on both sides.

- **`json`** — ~6,100 inputs (100k-deep nesting both directions, circular containers, a `default=` that mutates the container mid-encode, `str`/`int`/`float`-subclass keys with lying `__eq__`/`__hash__`/`__str__`, `sort_keys` with a mutating `__lt__`, NaN/Inf, huge/negative/NaN `indent`, lone surrogates, hostile decoder hooks, negative and 10⁹ `scanstring` indices). **Zero crashes on either side, ASan clean.** Parity gaps only: C tolerates mid-encode dict mutation where Python raises `RuntimeError`; C survives 10k nesting where Python hits `RecursionError`.
- **`pickle`** — ~8,100 curated plus ~43k internal mutations, 20k random byte mutations and 20k random opcode sequences under ASan (`__reduce__` with wrong arity/types, `__reduce__` mutating the memo or the container mid-pickle, hostile `persistent_id`/`find_class`/`reducer_override`, ~30 hand-crafted corrupt streams with overflowing lengths and stack underflow on every stack op). **Zero crashes on either side, ASan clean.** 23 exception-type gaps where C raises `UnpicklingError` and the twin leaks `IndexError`/`KeyError`/`EOFError`.
- **`heapq`** — 31 inputs; a `__lt__` that clears/shrinks/grows the heap during every operation including the `_max` variants, `__lt__` raising on the Nth call, `__del__` clearing the heap, a same-size mutation forcing an `ob_item` realloc, a list subclass with a lying `__len__`. **Nothing crashed, ASan clean.** The C code is genuinely hardened: `siftdown`/`siftup` re-check `PyList_GET_SIZE` after *every* `PyObject_RichCompareBool` and re-fetch `_PyList_ITEMS` (`_heapqmodule.c:53/60/102/103`), and `heappushpop` re-checks size after the user compare (`:285`). A well-earned zero.
- **`zoneinfo` TZif parsing** — 4,000 random blobs, all 128 progressive truncation prefixes, lying header counts, negative and 2³⁰ counts, out-of-range type/designation indices, ~16 POSIX-TZ footer shapes. **All agreed between backends.**
- **Shared limitation, not a finding:** `_json.make_encoder(...)(obj, 10**9)` times out — but so does the pure-Python `_make_iterencode` at the same level. Both sides hang: a shared O(n²) indent blowup, not a C defect. Levels 0–10⁶ are correct on both.

### Pairs with no possible differential

`_csv` (`Lib/csv.py` does an unconditional `from _csv import ...` — no pure-Python reader/writer exists), `_struct` (`from _struct import *`), `_random` (`random.Random` is built *on* `_random.Random`; the `except ImportError` at `random.py:161` is unrelated), and `_collections`' `deque`/`defaultdict` (the `except ImportError` branches only guard the import). Within `_collections`, only `OrderedDict` and `_count_elements` have real twins — both covered above.

## Classes bounded (clean negatives, with evidence)

Stated as earned zeros, with the shapes actually tried.

- **`strftime` — clean (10 probes).** 200-digit field width, embedded NUL in the format, trailing bare `%`, unknown directives `%q%!%~`, a 100 000-directive format (400 KB output), a lone surrogate in the format, a `str` subclass whose `__str__` lies, `%Y|%y|%G` at year 1, `%j %Y %a` on a `time`. **All agree.**
- **`__new__`-bypass / uninitialized payload — clean (9 probes).** `date.__new__(date)`, `datetime.__new__`, `time.__new__`, `timedelta.__new__`, `tzinfo.__new__(...).utcoffset(None)`, `timezone.__new__(...)` × {`utcoffset`, `tzname`, `repr`}, `IsoCalendarDate.__new__`. Both backends raise or return identically; no garbage-payload read. The `init-bypass` shape does **not** apply to `_datetimemodule.c`.
- **Hostile `tzinfo` — clean (10 probes).** `utcoffset` returning an `int`, a `timedelta(days=10**6)`, a lying `timedelta` subclass, or raising on the *second* call; `tzname` returning a non-`str` or a string with an embedded NUL; a `utcoffset` that re-enters `astimezone`; `fromutc` returning a non-datetime; `timezone(timedelta)` at the ±24 h boundary and far beyond. **All agree.**
- **Timestamp / ordinal extremes — clean (10 probes).** `fromtimestamp` with `1e300`, `-1e300`, `nan`, `inf`, `2**63`; `fromordinal(0)`, `(-1)`, `(10**30)`; `datetime.fromordinal(0)`; `date.fromtimestamp(nan)`. Both raise the same type except the `OverflowError`/`ValueError` class above.
- **`__reduce__` round-trips and `__setstate__` — clean (8 probes).** `date`/`datetime`/`timedelta`/`timezone` round-trips, `__setstate__` with a 1-byte and a 2-byte state, a state tuple carrying `42` as `tzinfo`. Both reject the malformed states. Notably: `__setstate__` **is** length-checked; only the *constructor* backdoor is not.
- **ISO parsers, memory safety — clean (360 000 trials).** Randomized differential fuzz of `time`/`date`/`datetime` `.fromisoformat` over digits, separators, `Z T W + - , .`, spaces, `\x00`, `\x01`, `\t`, `\n`, a non-ASCII char and a lone surrogate. **Zero crashes on either backend.** 1 320 divergences, all non-crashing, and **zero** cases where both accepted and produced *different values*. Direction split: 1 274 (96.5 %) are the pure-Python twin being *more* permissive (it tolerates `\t`, e.g. `'6420\t44'` → `date(6420,4,4)`) — twin laxity, not a C bug; 40 are C-accepts/twin-rejects, and those are exactly the two mechanisms in the CONSIDER above. This also bounds the latent `correction[to_parse-1]` hazard at `:1080` (`to_parse` is `size_t`; `to_parse == 0` would index `correction[SIZE_MAX]`): unreachable in 360 k trials, and the `p >= p_end` guard on the decimal-mark path dominates it.
- **No scope escape into `Modules/timemodule.c`.** `date(...).timetuple()` yields `tm_wday = -1` on release; `time.asctime`, `time.strftime('%a %A %U %W')` and `time.mktime` all **normalize** it and read in bounds under ASan. The negative weekday is contained in `_datetimemodule.c`.
- **Blast radius of the year-0 object is bounded to `ctime()`.** Every public method of every out-of-range `date`/`time`/`datetime` the backdoor can build was run under release+ASan. Only `date.ctime()`/`datetime.ctime()` are memory-unsafe. `toordinal`, `weekday`, `isoweekday`, `isocalendar`, `timetuple`, `strftime` return wrong-but-safe values on release (and trip the same debug assert); `repr`, `hash`, `isoformat`, arithmetic and a pickle round-trip are fully clean.

## Toolkit assessment

`find_parity_pairs.py` **found the right pair and then stopped**. It correctly ranked `datetime` at `high` with the exact `python_twin_module`, `c_sources` and `force_python_hint` I needed — that part earned its keep, and it is why the crown-jewel finding exists. Everything after "which pair" I had to build myself.

### Precision per rule

| tier | pairs | assessment |
|---|---|---|
| `high` (`explicit_py_twin`) | 6 | 5 real (`datetime`, `decimal`, `io`, `abc`, `warnings`). **1 false pair: `long`.** `c_module: "_long"` **does not exist** — `import _long` → `ModuleNotFoundError`. And `_pylong` is an *algorithm helper* (`int_from_string`, `int_to_decimal`, `str_to_int`), not an API twin of `Objects/longobject.c`; there is no shared public surface to drive a differential over. A fabricated module name at the **highest** confidence tier is the worst kind of precision loss, because it is the tier an agent is told to start with. |
| `medium` (`import *`) | 12 | plausible; `heapq`/`bisect`/`operator` are genuine inline-fallback families. Not individually validated. |
| `low` (`named`) | 21 | **systematically under-rated.** `json` (`c_make_scanner`/`py_make_scanner`, `c_make_encoder`), `pickle` (`Pickler is not _Pickler`) and `csv` have real, side-by-side dual paths — they are simply selected by a *name rebinding at the bottom of the module* rather than by a separate `_py*` file. Rating them `low` tells the agent to deprioritize exactly the pairs in this review's sample. |

### Recall gaps found by reading

**`zoneinfo` is a missed `high`-confidence twin, and its C file is in the 12-file sample.** `Lib/zoneinfo/_zoneinfo.py` is a 25 KB full pure-Python implementation; `Lib/zoneinfo/__init__.py` does the canonical `try: from _zoneinfo import ZoneInfo / except ImportError: from ._zoneinfo import ZoneInfo`. Both classes are importable side by side and share **34** public API members. The scanner emits `python_twin_module: null, confidence: low` — because the `_py*` heuristic only matches `Lib/_py<name>.py` and never `Lib/<pkg>/_<pkg>.py`. Given `Modules/_zoneinfo.c` (2 824 lines of TZif binary parsing) was picked for this sample partly for its parse surface, this is the single most costly miss — **and the cost is now measured, not hypothetical: CPY-0033 came out of that pair, and it exists only because I identified the twin by hand after the scanner said there wasn't one.** An agent that trusted the `low`/`null` classification would have skipped `zoneinfo` entirely and reported a clean sample.

### Prompt problems

- **The agent prompt's backend-confirmation advice is wrong for the flagship pair.** It suggests `datetime.datetime.__module__` — which is `'datetime'` for **both** backends. An agent following it verbatim would run a "differential" where both sides are the C accelerator and report a false clean. (The run brief hedged with "or similar"; the agent file should not.)
- **No guidance on which build to use.** A release build silently returns wrong values where a debug build aborts and an ASan build names the exact array. This finding looks like three different bugs across the three builds. The prompt says "prefer a debug or ASan build" but does not say **run the same input on all of them and report the matrix** — which is what turns "a debug assert" into "a release SIGSEGV plus an info leak".

### Ranked tuning proposals

1. **Ship the harness, not just the inventory.** Add `--emit-harness <pair>` to `find_parity_pairs.py` (or a `scripts/parity_harness.py`) that writes a ready-to-run differential: subprocess-per-trial, exit-code decoding (`-11`/`139` SIGSEGV, `-6`/`134` SIGABRT, TIMEOUT), a verdict classifier, and both preludes. This is the single biggest lever — ~90 % of my elapsed time went into rebuilding it, and it is identical for every pair.
2. **Add a `backend_assertion` field per pair, and make it mandatory in the emitted prelude.** For `datetime`: `type(m.datetime.replace).__name__ == 'method_descriptor'` vs `'function'`. Derive it generically (a C accelerator's methods are `method_descriptor`/`builtin_function_or_method`; a pure-Python twin's are `function`/`method`). Also add the missing `force_c_hint` — the JSON has only `force_python_hint`. A differential that cannot prove its two sides differ is worthless, and the current output cannot prove it.
3. **Fix the twin-discovery heuristic and the confidence ladder.** (a) Also match `Lib/<pkg>/_<pkg>.py` — recovers `zoneinfo` at `high`. **This one rule change is worth a FIX finding per run on this sample alone** (CPY-0033). (b) Verify `c_module` is actually importable before emitting it — kills the `long` false pair. (c) Promote a pair to at least `medium` when the dispatcher module binds *both* a `py_*`/`_*` and a `c_*` name for the same symbol (`py_make_scanner`/`c_make_scanner`, `_Pickler`/`Pickler`) — recovers `json`, `pickle`, `csv` from `low`.

Two smaller ones: add a per-pair `probe_hints` list (for `datetime`, "constructors accept a raw pickle state that skips field validation" would have pointed straight at this bug), and have the agent prompt require a **build matrix** row (debug / release / ASan) for every reproduced crash rather than a single transcript.
