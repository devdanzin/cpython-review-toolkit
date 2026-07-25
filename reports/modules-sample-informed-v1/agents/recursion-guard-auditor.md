# recursion-guard-auditor — `Modules/` sample (informed)

**Toolkit:** cpython-review-toolkit v0.8.0 · `scan_recursion_guards.py`
**Target:** `/home/danzin/projects/cpython` main @ `4f3be1b5777` (3.16.0a0)
**Repro builds:** `python_build_matrix/builds/debug-gil-nojit{,-asan}/python` @ `a1d580430c8`

**Line-number reconciliation:** every file cited below
(`_datetimemodule.c`, `_collectionsmodule.c`, `_randommodule.c`, `_json.c`, `_pickle.c`,
`_elementtree.c`, `_functoolsmodule.c`, `_sqlite/row.c`, `Objects/object.c`,
`Objects/tupleobject.c`, `Include/internal/pycore_object.h`) is **byte-identical**
between the build commit `a1d580430c8` and the read commit `4f3be1b5777`
(`git diff --quiet` clean). Source line numbers and gdb/ASan frame line numbers
therefore agree exactly — no reconciliation was needed or performed.

**Organizing fact (applied, not re-derived):** `PyObject_Hash`
(`Objects/object.c:1158`) dispatches straight to `tp_hash` with no
`_Py_EnterRecursiveCallTstate`; `PyObject_Repr` (:759) / `PyObject_Str` (:800) /
`PyObject_RichCompare` (:1099) all wrap it. Re-read at `4f3be1b5777` to confirm the
scanner's `dispatcher_guard_model` is still accurate — it is.

---

## Scanner volume

| | sample (12 files) | whole `Modules/` |
|---|---|---|
| raw | 5 | 13 |
| `missing_recursion_guard` | 4 | 11 |
| `recursion_descent_guarded_by_dispatcher` | 1 | 2 |
| confirmed FIX | **0** | **1** (`pysqlite_row_hash`, scope escape) |
| dismissed with a stated bound | 3 | 5 |
| correctly self-labelled non-bug (`hash_entry_point` / dispatcher) | 2 | 6 |

**Precision (sample):** 0 / 4 `missing_recursion_guard` are bugs — but 3 of those 4
are `hash_entry_point` at `confidence: low`, which the scanner's own detail string
already says "is not a bug in itself". Counting only claims the scanner actually
makes: **1 mis-rated finding (`delta_hash`, `high`) out of 5** → precision on
`high` in-sample **0/1 = 0%**, on `low` **4/4 = 100%**.

**Precision (whole `Modules/`, `high` only):** 1/4 = 25%, and the one true positive
(`pysqlite_row_hash`) is a **reproduced SIGSEGV** that the 12-file sample filter
discarded.

---

## Findings

### [FIX] `pysqlite_row_hash` descends both `sqlite3.Row` fields with no recursion guard — `Modules/_sqlite/row.c:235`, `:239`

**Scope note:** outside the 12-file sample; present in
`scanners/scan_recursion_guards.Modules.json` at `confidence: high`,
`shape: field_element_descent`, both sites listed. Chased per the briefing's
"chase scope escapes" rule.

**What:**

```c
static Py_hash_t
pysqlite_row_hash(PyObject *op)
{
    pysqlite_Row *self = _pysqlite_Row_CAST(op);
    Py_hash_t hash_description = PyObject_Hash(self->description);   /* :235 */
    if (hash_description == -1) {
        return -1;
    }
    Py_hash_t hash_data = PyObject_Hash(self->data);                  /* :239 */
    if (hash_data == -1) {
        return -1;
    }
    return hash_description ^ hash_data;
}
```

Registered as `{Py_tp_hash, pysqlite_row_hash}` in `row_slots` (`row.c:274`). No
`Py_EnterRecursiveCall` anywhere in `Modules/_sqlite/` (verified by grep). The
load-bearing site is **`:239`** — `self->data` is an arbitrary tuple, and a
`sqlite3.Row` can be an element of it, so the descent is *additive*:
`pysqlite_row_hash` → `tuple_hash` → `pysqlite_row_hash` → … Two unguarded C
frames per nesting level. `:235` is bounded in practice (`description` is copied
from the cursor) but is the same defect.

**Guarded twin — in the same file, over the same two fields:**
`pysqlite_row_richcompare` (`row.c:247`) descends `self->description` /
`self->data` via `PyObject_RichCompareBool` (`:257`) and `PyObject_RichCompare`
(`:261`), which *are* dispatcher-guarded at `Objects/object.c:1099`. Same object
graph, same fields, one operation guarded and the other not — the textbook
asymmetry. Confirmed A/B below.

**Reachability:** `sqlite3.Row.__new__(cursor, data)` takes `data` as
`object(subclass_of='&PyTuple_Type')` (`row.c:74`) — a plain, unvalidated tuple.
Three lines of stdlib Python:

```python
con = sqlite3.connect(":memory:"); cur = con.execute("select 1 as a")
r = sqlite3.Row(cur, (1,))
for _ in range(400_000): r = sqlite3.Row(cur, (r,))
hash(r)
```

**Confirmed — SIGSEGV, exit 139**, `debug-gil-nojit`:

```
=== sqlite3.Row N=400000 ===
built 400000
exit=139
```

**gdb frames** (`ulimit -s 1024`, N=60000, `debug-gil-nojit`), showing the
alternating descent and the site line numbers matching the source exactly:

```
Program received signal SIGSEGV, Segmentation fault.
0x000055555575e418 in _Py_TYPE_impl (ob=0x7ffff74f20d0) at Objects/tupleobject.c:373
#0  0x000055555575e418 in _Py_TYPE_impl (ob=0x7ffff74f20d0) at Objects/tupleobject.c:373
#1  tuple_hash (op=0x7ffff74f20d0) at Objects/tupleobject.c:374
#2  0x00007ffff72f3a03 in pysqlite_row_hash (op=0x7ffff6999d60) at ./Modules/_sqlite/row.c:235
#3  0x000055555575e489 in tuple_hash (op=0x7ffff6999db0) at Objects/tupleobject.c:385
#4  0x00007ffff72f3a15 in pysqlite_row_hash (op=0x7ffff6999e00) at ./Modules/_sqlite/row.c:239
#5  0x000055555575e489 in tuple_hash (op=0x7ffff6999e50) at Objects/tupleobject.c:385
#6  0x00007ffff72f3a15 in pysqlite_row_hash (op=0x7ffff6999ea0) at ./Modules/_sqlite/row.c:239
#7  0x000055555575e489 in tuple_hash (op=0x7ffff6999ef0) at Objects/tupleobject.c:385
#8  0x00007ffff72f3a15 in pysqlite_row_hash (op=0x7ffff6999f40) at ./Modules/_sqlite/row.c:239
...
#21578 0x00005555555f10d5 in _start ()
```

(21 579 frames total; ~2 frames per nesting level as predicted.)

**Guarded-twin A/B at the identical depth** — same two 400 000-deep `Row` graphs,
`__eq__` instead of `__hash__`:

```
=== guarded twin: Row.__eq__ at the SAME depth that segfaults Row.__hash__ ===
built 400000
eq: RecursionError -> Stack overflow (used 16312 kB) in comparison
exit=0
```

Clean `RecursionError`, exit 0. The only difference between the two runs is which
of the two dispatchers the descent goes through.

**Class:** **FIX**. Native C-stack overflow → uncatchable SIGSEGV from pure
stdlib Python, no third-party code, no `ctypes`. Same shape as CPY-0020 (`ga_hash`,
reproduced) and CPY-0001 (`tuple_hash` / gh-154318).

**Fix:** bracket both descents with
`Py_EnterRecursiveCall(" while hashing")` / `Py_LeaveRecursiveCall()`, as
`ga_hash` and `tuple_hash` need. Note this is *additive* on top of the
`tuple_hash` fix: fixing gh-154318 alone still leaves `Row(cur,(Row(cur,(…))))`
crashing through the `row.c` half of the alternation, because `tuple_hash`'s guard
would bound the tuple frames while the `pysqlite_row_hash` frames stay unbounded
only if the guard is missing on one side. Both sides need it, or the guard must
land on the one shared choke point (`PyObject_Hash` itself).

---

## New siblings of known shapes

### [Recall gap, inventory] `_count_elements` hashes a caller-supplied key through an *alias* the scanner cannot see — `Modules/_collectionsmodule.c:2592`

```c
hash = _PyObject_HashDictKey(key);        /* :2592 */
```

`_PyObject_HashDictKey` (`Include/internal/pycore_object.h:840-849`) is a
`static inline Py_ALWAYS_INLINE` wrapper: a `PyUnicode_CheckExact` cached-hash fast
path, then **`return PyObject_Hash(op);`**. It is `PyObject_Hash` under another
name, and it is **not in the scanner's `_UNGUARDED_DISPATCH` vocabulary**
(`scan_recursion_guards.py:120`), so this site — inside one of the 12 sample files —
was never reported.

This is a `hash_entry_point`, so **not a bug in itself** (per the triage rules) —
but it is the highest-traffic one in the sample: `collections.Counter` /
`Counter.update()` route straight into it.

**Confirmed** — `debug-gil-nojit-asan`, exit 1:

```
built 400000
AddressSanitizer:DEADLYSIGNAL
==4042626==ERROR: AddressSanitizer: stack-overflow on address 0x7ffdd4af1ff8 ...
    #0 ... in tuple_hash .../Objects/tupleobject.c:385:27
    #1 ... in tuple_hash .../Objects/tupleobject.c:385:27
    ... (246 identical frames, ASan cap)
SUMMARY: AddressSanitizer: stack-overflow .../Objects/tupleobject.c:385:27 in tuple_hash
```

and the exact entry chain under gdb (`ulimit -s 1024`, N=60000):

```
#16018 0x000055555575e489 in tuple_hash (op=0x7ffff6ff3840) at Objects/tupleobject.c:385
#16019 0x00005555559bbc4e in _PyObject_HashDictKey (op=0x7ffff6ff3840) at ./Include/internal/pycore_object.h:848
#16020 _collections__count_elements_impl (module=..., mapping=..., iterable=...) at ./Modules/_collectionsmodule.c:2592
#16021 _collections__count_elements (module=..., args=0x7fffffff9588, nargs=2) at ./Modules/clinic/_collectionsmodule.c.h:593
```

Reproducer: `x = ()` then `for _ in range(400_000): x = (x,)` (never hashed, so
`ob_hash` stays `-1` at every level and `tuple_hash`'s memo cannot short-circuit),
then `collections.Counter([x])`.

**Blast radius of the alias:** 27 `_PyObject_HashDictKey` / `PyObject_GenericHash`
call sites tree-wide, across `Objects/dictobject.c` (8+), `Objects/typeobject.c:6147`
and `Modules/_collectionsmodule.c` — all invisible to the current vocabulary.
**Class:** not a finding on its own; a **scanner recall gap** (proposal T1) plus
blast-radius evidence for CPY-0001.

### Other confirmed entry points (blast radius, not bugs)

Both reproduced at N=400 000 on `debug-gil-nojit`, both **exit 139**:

- `Modules/_randommodule.c:329` `random_seed` — `PyObject_Hash(arg)`.
  **Not reachable via `random.seed()`**: `Lib/random.py:170` restricts the seed to
  `None/int/float/str/bytes/bytearray` and raises `TypeError`. Reachable via
  `_random.Random().seed(deep_tuple)`, which segfaults.
- `Modules/_functoolsmodule.c:1311` `infinite_lru_cache_wrapper` — a
  `@functools.lru_cache(maxsize=None)`-decorated function called with a deep tuple
  argument segfaults at call time.

Neither is a bug in itself; both are convenient triggers and evidence that a single
`tuple_hash` fix retires several public-API crashes at once.

---

## Classes bounded (clean negatives, with the bound)

### The 4 in-sample `missing_recursion_guard` candidates

- **`delta_hash` (`Modules/_datetimemodule.c:2568`) — bound 1. FALSE POSITIVE at `high`.**
  The hashed value is `temp = delta_getstate(self)`, and `delta_getstate`
  (`:3022-3027`) is exactly `return Py_BuildValue("iii", GET_TD_DAYS(self),
  GET_TD_SECONDS(self), GET_TD_MICROSECONDS(self));` — a freshly built 3-tuple of C
  `int`s. One `tuple_hash` frame over three `long_hash` frames; nothing in it is a
  user object. This is the `temporary_container_descent` shape, not
  `field_element_descent`, and the scanner missed it only because the fresh
  container is returned by a *file-local helper* rather than constructed inline
  (proposal T2).

- **`time_hash` (`:5145`) — bound 2.** `temp2 = delta_subtract(temp1, offset)`.
  `delta_subtract` (`:2590`) returns `new_delta(days, seconds, microseconds, 1)` —
  always a **base** `timedelta`, never a subclass, regardless of what `offset` was.
  So `PyObject_Hash(temp2)` = `delta_hash` = bound 1, +1 frame = **bound 2**.

- **`datetime_hash` (`:6724`) — bound 2.** Structurally identical; same
  `delta_subtract` → base `timedelta` argument.

- **`random_seed` (`Modules/_randommodule.c:329`)** — correctly typed
  `hash_entry_point` at `low`. Not a bug (adds one frame). Listed above as a
  reproducer.

### The `tzinfo` re-entry route (the briefing's "self-recursion distinct from container descent")

`time_hash`/`datetime_hash` call `time_utcoffset`/`datetime_utcoffset`, which reach
user Python through `call_tzinfo_method` (`:1100`). A `tzinfo` whose `utcoffset()`
re-enters `hash()` on another aware object *is* a recursion route — but it passes
through the eval loop, and `_Py_EnterRecursiveCallTstate`
(`Include/internal/pycore_ceval.h:218-233`) is a real **C-stack-pointer** check
(`here_addr < _tstate->c_stack_soft_limit`) since 3.14, so every Python frame is a
checkpoint. **Bounded — confirmed as a negative**, `debug-gil-nojit`:

```
--- datetime_hash via tzinfo.utcoffset ---
RecursionError (clean): Stack overflow (used 16317 kB)
exit=0
--- time_hash via tzinfo.utcoffset ---
RecursionError (clean): Stack overflow (used 16315 kB)
exit=0
```

(`sys.setrecursionlimit(100000)`; `class Evil(datetime.tzinfo)` whose `utcoffset`
hashes a fresh `datetime`/`time` carrying another `Evil()`.)

`call_tzinfo_method` additionally rejects any non-`timedelta` return with a
`TypeError`, so the value that eventually reaches `PyObject_Hash` is still bounded.

### Remaining `tp_hash` slots in the sample (read by hand, none reported)

- **`date_hash` (`_datetimemodule.c:4095` slot) — bound 0.** `generic_hash(self->data, …)`
  → `Py_HashBuffer` over the raw 4-byte date payload. No Python object involved.
- **`timezone_hash` — bound 2.** `return delta_hash(self->offset);` — `offset` is
  validated to be a `timedelta` at construction; `delta_hash` is bound 1.
- **`deque` and both `_pickle` types — bound 0.** Registered as
  `{Py_tp_hash, PyObject_HashNotImplemented}` (`_collectionsmodule.c:1870`,
  `_pickle.c:5228`, `:7728`).

### The three deep-descent modules the briefing asked about — all guarded, all verified

Built a 300 000-level nested structure for each and ran on `debug-gil-nojit`
(`sys.setrecursionlimit(200000)`); every one raises a clean `RecursionError`, exit 0:

```
json.dumps  (encoder_listencode_obj): RecursionError -> Stack overflow (used 16312 kB) while encodin
pickle.dumps (_pickle save)         : RecursionError -> Stack overflow (used 16312 kB) while picklin
json.loads  (scan_once_unicode)    : RecursionError -> Stack overflow (used 16312 kB) while decodin
ET deepcopy (_elementtree deepcopy): RecursionError -> Stack overflow (used 16312 kB) in Element.__
ET itertext (elementiter_next)     : NO ERROR
exit=0
```

- **`Modules/_json.c` — both directions guarded.**
  *Encoder:* `encoder_listencode_obj` brackets **all three** of its recursive
  descents — list/tuple `:1605`, dict `:1612`, and the `default=` callback result
  `:1643`. A 7-node call-graph SCC
  (`encoder_listencode_obj` / `_dict` / `_list` / `encoder_encode_key_value` /
  the three `_lock_held` iterators) has no unguarded entry: every cycle passes
  through `encoder_listencode_obj`.
  *Decoder:* `scan_once_unicode` guards object `:1131` and array `:1139`; the
  3-node SCC (`scan_once_unicode` / `_parse_object_unicode` / `_parse_array_unicode`)
  likewise cannot be entered without passing the guard. `object_hook` /
  `object_pairs_hook` re-entry goes through the eval loop and is bounded there.

- **`Modules/_pickle.c` — the depth notion *does* bound the C stack, not just the memo.**
  The briefing's question resolves cleanly: the bound is
  `_Py_EnterRecursiveCall(" while pickling an object")` at **`:4612`**, released at
  **`:4765`**, bracketing the entire non-atomic dispatch inside `save()` itself —
  a real C-stack-pointer check, not a memo counter. The 20-node save-side SCC
  (`save` / `save_dict` / `save_list` / `save_reduce` / `batch_*` / …) is entered
  only through `save`. The additional guards at `:3293`, `:3302`, `:3600`, `:3612`
  are belt-and-braces around the `batch_*` helpers.
  *Load side:* the call-graph analysis finds **no cycle at all** — the `Unpickler`
  is an iterative stack machine (`Pdata` + `do_append`), so it cannot overflow the
  C stack this way.

- **`Modules/_elementtree.c` — guarded where it recurses, iterative where it doesn't.**
  The only C recursion is the 2-node
  `_elementtree_Element___deepcopy___impl` ↔ `deepcopy` cycle, guarded at
  `:822` / released at `:914`, `:918`. Serialization lives in
  `Lib/xml/etree/ElementTree.py`, not C. `iter()`/`itertext()` use
  `create_elementiter`'s explicit heap `parent_stack` (`elementiter_next`,
  `:2230`) — genuinely iterative, no C recursion; the 300 000-deep `itertext()`
  above completed with no error.

### `recursion_descent_guarded_by_dispatcher` (not re-litigated)

- `deque_richcompare` (`Modules/_collectionsmodule.c:1706`) — `PyObject_RichCompareBool`,
  guarded at `Objects/object.c:1099`. Correct.
- `array_richcompare` (`Modules/arraymodule.c:924`, whole-`Modules/` only) — same. Correct.

Neither is reachable by a route that bypasses `PyObject_RichCompare`.

### `tp_repr` / `tp_str` in the sample — all bounded

- `element_repr` (`Py_ReprEnter` `:1730`), `deque_repr` (`Py_ReprEnter` `:1638`),
  `defdict_repr` (`Py_ReprEnter` `:2387`) — explicitly guarded.
- `count_repr`, `repeat_repr`, `tuplegetter_repr`, `s_repr`, `zoneinfo_repr/_str`,
  and the five `_datetimemodule.c` reprs use `%R` in `PyUnicode_FromFormat`, which
  routes through `PyObject_Repr` — dispatcher-guarded (`Objects/object.c:759`).
  Per the FP taxonomy, indirect repr spellings are still guarded.
- **`defdict_repr:2380` `PyDict_Type.tp_repr(op)` is a genuine dispatcher bypass**
  (a direct static-slot call, not `PyObject_Repr`) — but `dict_repr` itself calls
  `PyObject_Repr` on every key and value, so each nesting level still takes one
  guard increment. It costs 2 unguarded C frames per level instead of 1.
  **Bounded — ACCEPTABLE**, worth knowing it exists.
- `_json.c:1532`/`:1593`/`:1689` `PyFloat_Type.tp_repr` / `PyLong_Type.tp_repr` are
  the same bypass shape on **bound-0** operands (a float, an int). ACCEPTABLE.

### Self-recursion sweep across all 12 files

An intra-file Tarjan SCC pass over the sample found exactly four real recursion
cycles — the `_json` encoder (7), the `_json` decoder (3), the `_pickle` saver (20),
and the `_elementtree` deepcopy (2). **All four are guarded** (above). Every other
"self-recursive" hit was a `#define` immediately following the function it wraps
(`new_date_ex`, `new_delta_ex`, `microseconds_to_delta_ex`, `_set_joined_ptr`,
`get_struct_state`, `get_random_state`, `Pdata_pop/push`, `find_state_by_type`),
not real recursion.

One genuine self-call is real but **bound 1**:
`deque_extendleft_impl` (`Modules/_collectionsmodule.c:534`) self-calls at `:545`
to handle `d.extendleft(d)` — but only after `s = PySequence_List(iterable)`, so the
recursive call always has `(PyObject *)deque != iterable` and terminates
immediately. **Bound: 1.**

### Suppressions the scanner made that I verified are correct

- `Modules/_interpchannelsmodule.c:2495` `PyObject_Hash(pyid)` — `pyid` is a fresh
  `PyLong`. **Bound 0**, correctly dropped by `_SCALAR_CTOR_RE`.
- `Modules/_decimal/_decimal.c:5846` `PyObject_GenericHash` — pointer/identity hash.
  **Bound 0**. Correctly not reported, but only by accident (see T1).

### One whole-`Modules/` `high` finding dismissed, with a *reproduced* bound

- **`pattern_hash` (`Modules/_sre/sre.c:3308`) — bound 1.** The FP taxonomy asserts
  `self->pattern` is "always a `str`/`bytes`". That assertion looked fragile,
  because the clinic signature is `_sre.compile(pattern: object, …)` — the
  annotation is **unconstrained `object`**, so the bound appeared to depend on
  `re.compile` politeness rather than on validation. Tested it directly:

  ```
  === _sre pattern_hash N=400000 ===
  TypeError: expected string or bytes-like object, got 'tuple'
  exit=1
  ```

  `_sre_compile_impl` validates the pattern internally, so the taxonomy entry
  **stands** — but it now stands on a measurement rather than on the clinic
  annotation. Recorded so the next run does not re-open it.

- **`ndarray_hash` (`Modules/_testbuffer.c:2627`, `:2636`) — POLICY, not FIX.**
  Structurally the same unguarded `field_element_descent` as `pysqlite_row_hash`,
  but `_testbuffer` is a test-support module not built into a normal install and
  not importable by ordinary user code. Same defect, no shipped blast radius.

---

## Toolkit assessment

### Precision per rule

| rule / shape | sample | whole `Modules/` | verdict |
|---|---|---|---|
| `field_element_descent` (`high`) | 1 (`delta_hash`) | 4 | **1 TP / 4** tree-wide (`pysqlite_row_hash`); the in-sample one is an FP |
| `hash_entry_point` (`low`) | 3 | 7 | correctly self-labelled non-bugs — **100% correct classification**, 0 promoted |
| `guarded_dispatcher_descent` (`low`) | 1 | 2 | correct, not re-litigated |
| `temporary_container_descent` | **0 emitted** | 0 | **rule never fired** — should have fired on `delta_hash` |
| `self_recursion` | 0 | 0 | correct: all four real SCCs in the sample are guarded |
| `slot_helper_descent` | 0 | 0 | correct |

The `hash_entry_point` calibration is the standout success of v0.8: seven findings
that in a naive scanner would each read as a bug are all pre-labelled `low` with a
detail string that says outright they are not bugs. I promoted none of them and
used three as reproducers, exactly as the prompt prescribes. Zero triage time was
wasted on them.

### Recall gaps found by reading

1. **The `PyObject_Hash` alias is invisible.** `_UNGUARDED_DISPATCH`
   (`scan_recursion_guards.py:120`) is `frozenset({"PyObject_Hash"})`. CPython also
   spells the same operation `_PyObject_HashDictKey`
   (`Include/internal/pycore_object.h:840`, a `Py_ALWAYS_INLINE` wrapper whose tail
   is literally `return PyObject_Hash(op);`). **27 sites tree-wide** — including
   `Modules/_collectionsmodule.c:2592`, inside this very sample — are unreachable
   by the scanner. Missing this in `Objects/dictobject.c` (8+ sites) matters more
   than missing it in `Modules/`.

2. **`temporary_container_descent` does not survive one hop.** `_TEMP_CTOR_RE`
   (`:184`) only matches when the fresh container is constructed *in the argument
   expression*. `delta_hash` writes `PyObject *temp = delta_getstate(self);` and
   `delta_getstate` is a five-line file-local `Py_BuildValue("iii", …)`. Result: the
   shape degrades to `field_element_descent` and the confidence is promoted to
   `high` — the single worst-rated finding in the sample.

3. **`argument_kind: "unknown"` is a silent give-up.** `time_hash` and
   `datetime_hash` report `argument_kind: "unknown"` for `temp2`, a local assigned
   two lines earlier from a file-local call. The finding still landed at a
   defensible severity, but for the wrong reason — `unknown` falls through to
   `hash_entry_point`, which happens to be `low`. On a receiver-derived local it
   would fall through the same way and *under*-report.

4. **The sample filter discarded the run's only FIX.** `pysqlite_row_hash` was in
   `scan_recursion_guards.Modules.json` at `confidence: high` with both sites
   correctly listed — the scanner did its job. The 12-file pre-filter removed it. A
   process observation, but it decided the outcome of this run.

### Prompt problems

- The agent prompt's own FP-taxonomy example asserts `pattern_hash` is "bound 1,
  `self->pattern` is always a `str`/`bytes`". That is *true*, but only because of a
  validation buried in `_sre_compile_impl` — the clinic annotation is bare `object`.
  An agent that trusts the exemplar without testing learns the wrong dismissal
  reflex (trust the argument name), and an agent that distrusts it burns a repro
  cycle. The taxonomy entry should cite the *validation site*, not the field.
- The prompt's `field_element_descent` guidance says "arity has nothing to do with
  depth: one field is enough" — correct, and it is what made me chase
  `pysqlite_row_hash`. Worth keeping verbatim.
- Nothing in the prompt says the scanner's `sites[]` array already carries every
  unguarded call in the function. Both `row.c` sites were there and I nearly cited
  only `findings[].line` (`:235`, the *bounded* one). The prompt does warn "cite the
  load-bearing one, which is not always `findings[].line`" — that warning earned its
  place and should be promoted, not shortened.

### Ranked tuning proposals

**T1 (highest value) — teach the scanner CPython's `PyObject_Hash` aliases.**
Change `_UNGUARDED_DISPATCH` (`scan_recursion_guards.py:120`) to:

```python
_UNGUARDED_DISPATCH = frozenset({"PyObject_Hash", "_PyObject_HashDictKey"})
```

and add a separate bound-0 suppression set `{"PyObject_GenericHash", "Py_HashPointer",
"Py_HashBuffer"}` so those are dropped *deliberately* (with a recorded reason) rather
than by never being in the vocabulary. Recovers 27 tree-wide sites, 8+ of them in
`Objects/dictobject.c`. **Cost:** two lines. **Risk:** raises `hash_entry_point`
volume, which is already correctly rated `low`. Add a regression test asserting
`_collections__count_elements_impl` is reported.

**T2 — one-hop return-value propagation for `_TEMP_CTOR_RE` / `_SCALAR_CTOR_RE`.**
When a hashed local is assigned from a **file-local** function whose body is a single
`return <ctor>(…)`, classify the site by that constructor. Fixes `delta_hash`
(→ `temporary_container_descent`, `medium`) and would generalize to the whole
`*_getstate` family, which is a recurring CPython idiom. **Cost:** reuse the existing
function table the scanner already builds; match `^\s*return\s+<ctor>` in the callee
body. **Risk:** low — restrict to single-`return` bodies under ~5 statements and
never cross files.

**T3 — resolve intra-function local assignments before giving up to `unknown`.**
For `PyObject_Hash(x)` where `x` is a local, walk backwards for the dominating
assignment and re-run `argument_kind` classification on its RHS. Turns
`time_hash`/`datetime_hash`'s `"unknown"` into `"local_from_call:delta_subtract"`,
which T2 then bounds. Ordered after T2 because T2 is what makes the resolved kind
actionable.

**T4 — promote `field_element_descent` when the field is provably a container.**
When a hashed receiver field is typed by an Argument Clinic converter
(`subclass_of='&PyTuple_Type'`, `&PyList_Type`, `&PyDict_Type`, `&PyFrozenSet_Type`)
in the *same file's* `__new__`/`__init__`, emit a distinct
`nestable_field_descent` shape and keep `high`. `sqlite3.Row`'s
`data: object(subclass_of='&PyTuple_Type')` (`row.c:74`) is exactly this, and it is
what separates the one true positive from the three `high` false positives, all of
which hash `str`/`int`/fresh-tuple fields. This is the rule that would have made the
run's only FIX self-evident instead of requiring a scope escape.

**T5 (process, not code) — do not pre-filter `high`-confidence recursion findings
out of a sample.** The whole-`Modules/` JSON is already produced. For this agent
specifically, `high` findings outside the sample should be surfaced as a
"scope-escape candidates" section, because the class is small (13 tree-wide) and the
cost of reading four extra functions is minutes. The one FIX in this run came from
there.
