# recursion-guard-auditor — slice `obj-sequences` (Group A2, INFORMED)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** the 4 files in `preflight/slice_files.txt`
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`
**Build-matrix validity:** re-verified independently of the mapper and of Group A. `git diff a1d580430c8 4f3be1b5777 --` over the four slice files **plus** `Objects/object.c`, `Include/cpython/object.h`, `Include/internal/pycore_ceval.h` and `Python/bytecodes.c` is **empty**. Every line number below is valid on every matrix build without adjustment; that matters more than usual here, because the guard I am testing lives in `Objects/object.c` and `pycore_ceval.h`, outside the slice.

**Read first:** `preflight/RUN_CONTEXT.md`, `preflight/AGENT_BRIEF.md` §0, `agents/include-graph-mapper.md`, `agents/git-history-context.md`, `agents/refcount-auditor.md`, `agents/error-path-analyzer.md`, `preflight/informed_briefing.md`.

---

## Headline — the three answers, plainly

1. **The single finding is correctly classified, not a false positive.** `list_richcompare_impl` really is dispatcher-guarded; the 15 nesting/cycle scenarios that reach it give `RecursionError` on all four builds, never a signal.
2. **But the "1 of 1" framing is a category error, and the denominator is wrong.** `recursion_prone_slot_functions: 1` counts **`bytes_hash`**, not the reported finding. The denominator and the numerator are different functions in different files. The correct hand count of recursion-capable element-descent slots in this slice is **13 functions across 5 slot kinds**, of which the scanner's slot classifier recognises 13 by name suffix and **0 from any type table** — and the two slot kinds that matter most for the negative (`tp_hash` on `list`/`bytearray`, and `sq_contains`) are invisible to it.
3. **The `bytesobject.c` truncation is real but is NOT the cause.** `extract_functions` stops at `bytes_getnewargs` (`:2779`); 1,147 lines and 37 functions are invisible. I enumerated every definition in that region: **none is a `*_hash` / `*_repr` / `*_str` / `*_richcompare`, and none participates in a call-graph cycle.** The live hypothesis I was handed is **disproven** — the denominator of 1 would still be 1 with the truncation fixed.

**No FIX. No CONSIDER. One POLICY.** The slice contributes **zero nodes to the unguarded hash graph** — `list` and `bytearray` are unhashable and `bytes_hash` is a flat `Py_HashBuffer` — which is the structural reason `missing_recursion_guard: 0` here is earned rather than lucky.

**Positive control, so the negatives mean something.** On the same builds, with the same harness: `tuple_hash` (CPY-0001/gh-154318, PR #154362 still open) at depth 400 000 → **rc = -11 (SIGSEGV)**, and `ga_hash` (CPY-0020) at depth 400 000 → **rc = -11**, on `release-gil-nojit` **and** `debug-gil-nojit`. 4/4. The harness detects unguarded native recursion; it just did not find any here.

---

# 1. Findings

## [POLICY] The one scanner finding — correct as classified, and its own promotion clause *is* satisfied

`Objects/listobject.c:3467` — `list_richcompare_impl`, typed `recursion_descent_guarded_by_dispatcher`, `confidence: low`, `guarded_by: Objects/object.c:1099`.

**Triage verdict: TRUE POSITIVE for its own type. Not promotable to FIX. Kept as POLICY, for one specific reason given below.**

### The guard is genuinely present

```c
3459:    for (i = 0; i < Py_SIZE(vl) && i < Py_SIZE(wl); i++) {
3467:        int k = PyObject_RichCompareBool(vitem, witem, Py_EQ);   /* the descent */
...
3494:    PyObject *result = PyObject_RichCompare(vl->ob_item[i], wl->ob_item[i], op);
```

Both spellings route through `PyObject_RichCompare` (`Objects/object.c:1099`), whose body brackets `do_richcompare` with `_Py_EnterRecursiveCallTstate(tstate, " in comparison")` at **`Objects/object.c:1110`**. That guard is *not* the `sys.setrecursionlimit` counter: since 3.14 it is a real C-stack-pointer test — `Include/internal/pycore_ceval.h:211-220`, `here_addr < _tstate->c_stack_soft_limit`. So it bounds **pure-C** recursion, which is exactly what this bug class needs.

Measured (`repro/recursion_list_slot_matrix.py`, `RECUR_DEPTH=1000000`, i.e. **2.5× the depth at which the unguarded `tuple_hash` control segfaults on this host**):

| scenario | release-gil-nojit | debug-gil-nojit |
|---|---|---|
| `eq_deep` / `ne_deep` / `lt_deep` (two 10⁶-deep nests) | `RecursionError (Stack overflow (used 16344 kB) in comparison)`, rc=0 | same, rc=0 |
| `eq_cycle` / `lt_cycle` (`a=[];a.append(a)` vs `b=[];b.append(b)`) | `RecursionError`, rc=0 | rc=0 |
| `eq_self_cycle` (`a == a`) | `True`, rc=0 — identity fast path, `Objects/object.c:1128`, never descends | rc=0 |
| `contains_deep` / `index_deep` / `count_deep` / `remove_deep` | `RecursionError`, rc=0 | rc=0 |
| `sort_deep` / `sort_tuple_deep` / `sort_key_deep` | `RecursionError`, rc=0 | rc=0 |
| `eq_dunder_deep` / `lt_dunder_deep` / `eq_dunder_cycle` (`list.__eq__(a,b)`) | `RecursionError`, rc=0 | rc=0 |

Also 7/7 clean on `release-ft-nojit` and `debug-ft-nojit` under `PYTHON_GIL=0` — see §3.6.

### The promotion clause *is* met — two dispatcher-bypassing routes exist, and I measured one

The scanner's own text says: *"promote to a real finding only if you can show the slot is reached by a route that bypasses its dispatcher."* Two such routes exist:

**R1 — `Objects/listobject.c:2787`, inside `unsafe_object_compare`:**
```c
2783:    if (Py_TYPE(v)->tp_richcompare != ms->key_richcompare)
2784:        return PyObject_RichCompareBool(v, w, Py_LT);     /* guarded fallback */
2786:    assert(ms->key_richcompare != NULL);
2787:    res_obj = (*(ms->key_richcompare))(v, w, Py_LT);      /* DIRECT SLOT CALL */
```
`ms.key_richcompare` is set at `:3079` from `key_type->tp_richcompare` when `list.sort()`'s pre-sort check (`:3020-3086`) proves every key has the same exact type. Sorting a list of exact `list`s therefore calls `list_richcompare` **through a cached function pointer**, with no `PyObject_RichCompare` frame and no `_Py_EnterRecursiveCallTstate`.

Measured under gdb on `debug-gil-nojit` (`lst = [[[1],[2]], [[1],[3]]]; lst.sort()`):
```
$1 = (PyObject *(*)(PyObject *, PyObject *, int)) 0x5555556e49d0 <list_richcompare>
#0  unsafe_object_compare (v=…, w=…, ms=…) at Objects/listobject.c:2789
#1  count_run (ms=…, nremaining=2, slo=…)   at Objects/listobject.c:1983
#2  list_sort_impl (self=…, keyfunc=0x0, reverse=0) at Objects/listobject.c:3126
#3  list_sort (…)                            at Objects/clinic/listobject.c.h:273
```
No `PyObject_RichCompare` on the stack. This is the only direct `tp_*` slot dispatch anywhere in the four files (`grep` for `->tp_richcompare` / `->tp_repr` / `->tp_str` / `->tp_hash`: `listobject.c:2783/2787/3079` and nothing else).

**R2 — `Objects/typeobject.c:10253`, `wrap_richcmpfunc`:** `return (*func)(self, other, op);` — the slot wrapper behind `list.__eq__(a, b)` called from Python. Out of slice, but it reaches the in-slice slot.

### Why that still is not a FIX

Both routes add **exactly one** unguarded C frame. The *recursive step* is inside `list_richcompare_impl` and always re-enters through `PyObject_RichCompareBool` — `list_richcompare_impl` never calls `unsafe_object_compare`, and a user `__lt__` that starts a new sort costs a Python frame (guarded at `Python/ceval.c:1255`). The `sort_deep` / `sort_tuple_deep` / `eq_dunder_deep` rows above are that reasoning measured at depth 10⁶: `RecursionError`, rc=0, both builds.

**So: bound = +1 unguarded frame per `list.sort()` / `list.__eq__()` call. Not a bug.**

**Why POLICY and not ACCEPTABLE.** This is the case the informed briefing's *"guarded twins are twin for a specific threat model"* entry describes, and the agent brief's own POLICY definition: *"repr/str descents where the dispatcher is the only thing standing between the code and a segfault."* Here the dispatcher is already partly gone — `list.sort()` deliberately routes around it for speed — and the only remaining bound is that the recursive edge happens to be spelled `PyObject_RichCompareBool`. Any future fast path in `list_richcompare_impl` that reuses the cached `ms->key_richcompare` (the natural next optimisation, since the pre-sort check already proved type homogeneity) removes the bound silently. Worth one comment at `:2787` saying that the direct dispatch is safe *only because the callee's own descent re-enters through the dispatcher.* No code change proposed.

**Prior art: none.** `gh api -X GET search/issues` for `list richcompare recursion stack overflow`, `list_richcompare segfault nested`, `unsafe_object_compare recursion`, `list sort recursion crash nested list`, `bytearray recursion stack overflow`, `bytes __mod__ recursion segfault` → nothing on these files. The only in-family hits are gh-154318 / PR #154362 (`tuple_hash`, **open**, still segfaulting at this ref — my positive control) and gh-148268 (the `unsafe_*_compare` asserts, already Group A's F8).

---

# 2. The denominator — hand enumeration, and what `1` actually counts

## 2.1 `recursion_prone_slot_functions: 1` is **`bytes_hash`**, not the finding

`scan_recursion_guards.py:195` — `_RECURSION_PRONE_CALLER_SLOTS = {"tp_hash", "parameter_walk"}` — and `:1344/:1358` increment `slot_functions` only for a function whose classified slot is in that set. I re-ran the classifier over exactly the four files (`scratchpad/enumerate_slots.py`) and reproduced `classified_total: 13`, `from_slot_map: 0`, `from_name_suffix: 13`, `recursion_prone: 1` exactly. The one is:

> **`bytes_hash`, `Objects/bytesobject.c:1720`** — classified `tp_hash` by the `_hash` name suffix.

The reported finding, `list_richcompare_impl` (`tp_richcompare`), **is not in that denominator at all.** So `findings: 1 / recursion_prone_slot_functions: 1` is not a 1-of-1 precision measurement of one candidate; it is two unrelated counts that coincidentally both equal 1. The honest split:

| rule | denominator | fired | verdict |
|---|---|---|---|
| `missing_recursion_guard` (unguarded `PyObject_Hash` / self-recursion) | **1** — `bytes_hash` | 0 | **earned zero**, and I can name why: `bytes_hash` calls `Py_HashBuffer` (`bytesobject.c:1726`), which is in the scanner's own `_BOUND_ZERO_HASH` set. Bound **0**. |
| `recursion_descent_guarded_by_dispatcher` | **13** slot-classified functions | 1 | 1/1 correct; 12 correctly silent (they call no guarded dispatcher, or carry `Py_ReprEnter`) |

## 2.2 The classifier resolved **zero** slots from any type table — and that is where the real gap is

`_collect_slot_map` (`:321`) needs a *designated* initialiser (`.tp_hash = X`) or a `PyType_Spec` entry. All four slice type tables are **positional with trailing comments**:

```
listobject.c:3936      list_repr,                     /* tp_repr */
listobject.c:3940      PyObject_HashNotImplemented,   /* tp_hash */
listobject.c:3942      0,                             /* tp_str */
listobject.c:3950      list_traverse,                 /* tp_traverse */
listobject.c:3952      list_richcompare,              /* tp_richcompare */
listobject.c:3626      list_contains,                 /* sq_contains */
bytearrayobject.c:2915 0,                             /* tp_hash */
```

So `slot_map == {}` for every file and the classifier fell back entirely to name suffixes. Two consequences, both load-bearing for *this* slice:

- **The two facts that make the negative structural are invisible.** `list`'s `tp_hash` is `PyObject_HashNotImplemented` and `bytearray`'s is `0` — i.e. neither type can appear in a hash descent at all. Those live only in the positional table. The scanner cannot know them, so it cannot report *why* the zero is safe; it can only report that nothing matched.
- **`sq_contains` is not a slot the classifier knows** (`_SLOT_NAME_SUFFIX` covers only `_richcompare`/`_hash`/`_repr`/`_str`), so `list_contains`, `bytes_contains` and `bytearray_contains` are unclassified — and `sq_contains` is a genuine per-element descent.

One misclassification in the 13: **`_Py_bytes_repr` (`bytesobject.c:1442`)** is typed `tp_repr` by suffix, but it is a shared `(const char *, Py_ssize_t, …)` helper registered in no type table. Benign (produced no finding), but it is name-suffix guessing being wrong, and it is worth having in the fixture corpus.

## 2.3 The hand enumeration the task asked for

Every recursion-capable slot / comparison path in the four files. "Descends a user-controlled object graph?" is the only question that decides membership in the bug class.

| # | slot | function | site | descends? | guard | bound |
|---|---|---|---|---|---|---|
| 1 | `tp_repr` | `list_repr` → `list_repr_impl` | `listobject.c:636` / `:581` | **yes** — `PyUnicodeWriter_WriteRepr` per item (`:615`) | `Py_ReprEnter` `:583` / `Py_ReprLeave` `:625`,`:631` **for cycles**; `PyObject_Repr` `object.c:780` **for depth** (`WriteRepr` → `PyObject_Repr`, `unicode_writer.c:394`) | measured: `[...]` on a cycle, `RecursionError` at depth |
| 2 | `tp_str` | `list` — `tp_str == 0` | `listobject.c:3942` | inherits `tp_repr` via `PyObject_Str` `object.c:823` | same | measured (`str_deep` → *"while getting the repr of an object"*) |
| 3 | `tp_hash` | `PyObject_HashNotImplemented` | `listobject.c:3940` | **no** — raises `TypeError` | n/a | **0** |
| 4 | `tp_richcompare` | `list_richcompare` → `_impl` | `listobject.c:3501` / `:3438` | **yes** — `:3467`, `:3494` | `PyObject_RichCompare` `object.c:1110` | the §1 finding |
| 5 | `sq_contains` | `list_contains` | `listobject.c:656`, cmp at `:665` | **yes** | `PyObject_RichCompareBool` → `object.c:1110` | measured |
| 6 | `tp_traverse` | `list_traverse` | `listobject.c:3427` | **no** — `Py_VISIT` hands each item to a flat `visitproc`; the descent belongs to the GC, which is iterative | n/a | measured (`traverse_deep`, 50 000-long cycle, `gc.collect()` → rc=0) |
| 7 | `tp_dealloc` | `list_dealloc` | `listobject.c:555` | recurses only by `Py_DECREF`-ing contained objects | the automatic trashcan in `_Py_Dealloc` (FP-taxonomy entry: do **not** look for `Py_TRASHCAN_BEGIN`, it is an empty shim) | measured (`dealloc_deep`, 10⁶-deep nest dropped with no iterative unwind → rc=0) |
| 8 | sort compares | `safe_object_compare` `:2767`, `unsafe_object_compare` `:2778`, `unsafe_tuple_compare` `:2884` | `:2770`, `:2784`/`:2791`, `:2903`/`:2916` | **yes** | `PyObject_RichCompareBool` — **except `:2787`**, see §1 R1 | measured |
| 9 | `index`/`count`/`remove` | `list_index_impl` `:3345`, `list_count_impl` `:3381`, `list_remove_impl` `:3412` | | **yes** | `PyObject_RichCompareBool` | measured |
| 10 | `tp_repr` / `tp_str` | `bytes_repr` `:1516` → `PyBytes_Repr` `:1435` → `_Py_bytes_repr` `:1442`; `bytes_str` `:1522` | | **no** — flat byte range | n/a | **0** |
| 11 | `tp_hash` | `bytes_hash` | `bytesobject.c:1720` | **no** — `Py_HashBuffer(a->ob_sval, Py_SIZE(a))`, memoised in `ob_shash` | n/a | **0** |
| 12 | `tp_richcompare` | `bytes_richcompare` | `bytesobject.c:1657` | **no** — `memcmp` | n/a | **0** |
| 13 | `sq_contains` | `bytes_contains` `:1623` → `_Py_bytes_contains` `bytes_methods.c:602` | | one level of user `__index__` / `__buffer__`, no graph descent | eval loop `ceval.c:1255` | **1** (measured, §3.3) |
| 14 | `tp_repr` / `tp_str` | `bytearray_repr` `:1130` → `_lock_held` `:1114`; `bytearray_str` `:1140` | | **no** — flat byte range | n/a | **0** |
| 15 | `tp_hash` | `bytearray` — `tp_hash == 0`, `tp_richcompare` non-NULL, so no inheritance ⇒ unhashable | `bytearrayobject.c:2915` | **no** | n/a | **0** |
| 16 | `tp_richcompare` | `bytearray_richcompare` | `bytearrayobject.c:1152` | **no** — two `Py_buffer`s + `memcmp` | n/a | **0** |
| 17 | `sq_contains` | `bytearray_contains` `:1374` → `_Py_bytes_contains` | | as row 13 | eval loop | **1** |
| 18 | `tp_traverse` | `listiter_traverse` `:4059`, `listreviter_traverse` `:4213`, `striter_traverse` `bytesobject.c:3427`, `bytearrayiter_traverse` `bytearrayobject.c:2965` | | **no** — single `Py_VISIT` of `it_seq` | n/a | **0** |
| 19 | C self-recursion | `bytearray_setslice` `bytearrayobject.c:636`, recursive call `:653` | | **yes, once** — `values == self` ⇒ copy to a fresh private bytearray, recurse; the copy is `!= self`, so the branch cannot be taken again | structural | **1 frame** (measured, 10 000 iterations, rc=0) |
| 20 | C self-recursion | `bytearray_ass_subscript_lock_held` `:728`, recursive call `:806` | | **yes, once** — `values` is replaced by `PyByteArray_FromObject(values)`, an exact bytearray `!= self`, so `values == self \|\| !PyByteArray_Check(values)` is false next time | structural | **1 frame** (measured) |
| 21 | format engine | `_PyBytes_FormatEx` `bytesobject.c:628`, shared by `bytes.__mod__` `:2847` and `bytearray_mod_lock_held` `:2844` | | **no C recursion**; user hooks re-enter through Python frames | eval loop `ceval.c:1255` | measured, §3.3 |

**Whole-slice self-recursion / mutual-recursion sweep.** `scratchpad/self_recursion_sweep.py` and `scratchpad/callgraph_cycles.py` split all four files by brace balance over the **raw text** (deliberately not tree-sitter, so the truncated `bytesobject.c` tail is covered), giving **367 definitions** and a 339-node intra-slice call graph. Result: exactly the **two** self-loops in rows 19–20, and **zero** multi-node cycles. (My graph flagged one apparent `PyBytes_FromFormatV ↔ bytes_fromformat` cycle; it is a regex artefact — the "call" is the string literal `"PyBytes_FromFormatV(): %c format "` inside an error message at `bytesobject.c:273`. The scanner's AST-based cycle pass correctly does not report it. Recorded because it is my sweep's FP, not CPython's.)

---

# 3. Classes bounded (with denominators)

### 3.1 Unguarded `PyObject_Hash` descent — denominator 1, and it is structurally 0

`PyObject_Hash` (`Objects/object.c:1158`) dispatches straight to `tp_hash` with no guard — confirmed by reading the body at the review ref. The slice's contribution to that graph:

- `list` → `PyObject_HashNotImplemented` (`listobject.c:3940`) → `TypeError`. **Bound 0.**
- `bytearray` → `tp_hash == 0` with `tp_richcompare` set, so `inherit_slots` does not fill it in → unhashable. **Bound 0.**
- `bytes` → `bytes_hash` → `Py_HashBuffer` over `ob_sval`, memoised. **Bound 0.**

Measured (`repro/recursion_positive_control.py slice_types_are_not_hash_nodes`, both builds): `hash((deep_list,))` → `TypeError: unhashable type: 'list'`; `hash(bytearray(b'x'))` → `TypeError`; `hash(b'a'*100000)` fine. **These four files cannot appear anywhere in an unguarded hash chain.** That is the single strongest negative in this report, and it is why `missing_recursion_guard: 0` here is not luck.

### 3.2 Deep-nesting and cyclic descent through every list slot — 27 scenarios, both GIL builds, two depths

`repro/recursion_list_slot_matrix.py`, `RECUR_DEPTH` swept 200 000 → 1 000 000. **rc=0 on every scenario on `release-gil-nojit` and `debug-gil-nojit`** (full 27-scenario matrix at both 2·10⁵ and 10⁶), and on the 13 key scenarios at 10⁶ on debug. Every nesting scenario yields `RecursionError`; every cycle scenario yields either `RecursionError` or the correct cycle answer (`[...]`, `True`).

### 3.3 bytes/bytearray — the bounded negative, stated rather than assumed

**Neither type is a self-referential container.** A `bytearray` element must be an `int` in `range(256)`; a `bytes` element likewise. Measured (`recursion_bytes_format_paths.py no_nesting_possible`): `b[0] = bytearray(b"x")` → `TypeError: 'bytearray' object cannot be interpreted as an integer`; `bytes([bytes(1)])` → same shape. **So the classic element-descent shape is structurally absent, at depth 0.**

What *can* re-enter is the format engine and the coercion hooks. `repro/recursion_bytes_format_paths.py`, **20 scenarios × 2 builds, all rc=0**, covering every user hook `_PyBytes_FormatEx` reaches (`%b`/`%s` → `__bytes__` `:917`; `%d` → `__index__` `:965`; `%c` → `byte_converter` `:1003`; `%f` → `__float__` `:986`; `%a` → `PyObject_ASCII` `:904`; `%(key)s` → `PyObject_GetItem` `:750`), on **both** `bytes.__mod__` and `bytearray.__mod__`, plus `_PyBytes_FromList` / `FromTuple` / `FromIterator`, `bytearray.__setitem__`, and `__contains__` through both `__index__` and `__buffer__`. Every one is `RecursionError` (or `TypeError`, see below) — the bound is the eval loop's `_Py_EnterRecursiveCallTstate` at `Python/ceval.c:1255`, because each re-entry costs a Python frame.

**One result needed a second measurement to be honest about.** `contains_index_bytes` / `contains_index_bytearray` return `TypeError`, not `RecursionError` — which on its own would be a weak negative. `repro/recursion_bytes_format_paths.py contains_index_depth_counter` instruments the nesting: **peak `__index__` nesting 997** on both builds, then `TypeError`. The descent *did* reach the guard at ~1000; the `RecursionError` it raised is then swallowed by the **unnarrowed `PyErr_Clear()` at `bytes_methods.c:608`** and replaced by the `TypeError` from the retried `PyObject_GetBuffer`. That is the error-path agent's **F2**, observed independently from the recursion side, on the shared `bytes_methods.c` code path that backs both types. Not a new finding; a second, disjoint piece of evidence for theirs.

### 3.4 `list.sort` re-entrancy — the specific place I was told to check

The brief's point stands: `list_sort_impl:2968-2973` detaching `ob_item`/`ob_size`/`allocated` closes the *pointer* question and says nothing about recursion. I checked recursion three ways.

- **`__lt__` sorts the same list.** `sort_reentrant_same` → **completes, rc=0, both builds.** Mechanism, by reading: the re-entrant `list_sort_impl` sees `Py_SIZE(self) == 0` (detached), takes `nremaining < 2 → goto succeed`, and its epilogue restores `saved_allocated`, which is the outer sort's own `-1` marker — so the outer's "list modified during sort" check at the end still passes. Each re-entry is O(1) and returns. Depth is bounded by the Python frame guard, not by the detach.
- **`__lt__` starts a new sort every level.** `sort_reentrant_nested` → `RecursionError (maximum recursion depth exceeded)`, rc=0, both builds. Each level costs a Python frame.
- **`__lt__` performs a self-referential comparison mid-sort.** `sort_cmp_cycle` (`a` and `b` each containing themselves, compared from inside `Item.__lt__`) → `RecursionError`, rc=0, both builds. This is the case where the sort's *own* frame stack and the richcompare descent stack are both live; the guard still fires.
- **Sorting deeply-nested lists directly**, which is the case that actually exercises R1's direct slot dispatch: `sort_deep` (plain), `sort_tuple_deep` (`unsafe_tuple_compare` → `ms->tuple_elem_compare` → `unsafe_object_compare` → direct call), `sort_key_deep` (keyfunc) → all `RecursionError`, rc=0, at depth 10⁶ on both builds.

**Verdict: the recursion question on `list.sort` is clean, and the detach is irrelevant to it either way.**

### 3.5 `Py_ReprEnter` is a *cycle* guard, not a *depth* guard — and I checked whether that ever matters

`list_repr_impl` is the textbook `Py_ReprEnter`/`Py_ReprLeave` user, and the scanner's `_has_guard()` (`:357-359`) suppresses the whole function on that token. Correct here, but for the wrong reason if read carelessly: `Py_ReprEnter` is a per-thread *set* of objects currently being repr'd, so it catches `a.append(a)` and nothing else. The **depth** bound on `repr([[[…]]])` is `PyObject_Repr`'s `_Py_EnterRecursiveCallTstate` at `object.c:780`. The two are separately visible in my measurements: `repr_cycle` → `[[...]]` (that is `Py_ReprEnter`), `repr_deep` → `Stack overflow … while getting the repr of an object` (that is `object.c:780`, verbatim message). Two mechanisms, two threat models, one suppression token. See §4.3.

### 3.6 Free-threaded re-entrancy — no deadlock, and the reason

`list_repr` takes `Py_BEGIN_CRITICAL_SECTION(v)` (`:643`) and `list_richcompare` takes `Py_BEGIN_CRITICAL_SECTION2(v, w)` (`:3504`) **before** their `_impl` runs, so a self-referential `repr()` or `a == b` re-enters the same critical section on the same mutex(es) while already holding them. `PyMutex` is not reentrant, so this looked like a candidate deadlock. It is not: `_PyCriticalSection_End` (`Include/internal/pycore_critical_section.h:142-144`) documents *"the fast path in `_PyCriticalSection_BeginSlow` for locks already held in the top-most critical section"*, and `_PyCriticalSection2_End` (`:199-201`) the same for the pair. Measured, 7 cyclic scenarios × `release-ft-nojit` + `debug-ft-nojit` under `PYTHON_GIL=0`: **14/14 rc=0**, `RecursionError` or the correct cycle answer, no hang (120 s timeout). Recorded because the hypothesis was plausible and the code refutes it.

### 3.7 Deliberately not re-litigated

CPY-0001 / gh-154318 (`tuple_hash`) and CPY-0020 (`ga_hash`) are used **only as positive controls** here and are out of slice; both confirmed still crashing at this ref in one line each. CPY-0180, gh-153570, gh-153578, gh-153419, CPY-0014, gh-148268 belong to Group A and the mapper — not touched.

---

# 4. Toolkit assessment

### 4.1 Precision of the rule that fired: **1/1, correct.** Recall of its class in this slice: **1 of 3.**

The one `recursion_descent_guarded_by_dispatcher` row is right. But the shape's gate is `guarded_apis = call_names & _GUARDED_DISPATCH` plus `slot is not None` plus a loop, and `_GUARDED_DISPATCH` (`:157-165`) contains only `PyObject_Repr / Str / ASCII / RichCompare / RichCompareBool`. Two genuine element descents in this slice are therefore missed:

- **`list_contains` (`:656`, cmp at `:665`)** — a per-element `PyObject_RichCompareBool` loop, and `sq_contains` is not in `_SLOT_NAME_SUFFIX`, so `slot is None` and the shape cannot fire. `bytes_contains` / `bytearray_contains` likewise.
- **`list_repr_impl` (`:581`)** — descends via **`PyUnicodeWriter_WriteRepr`**, which is not in `_GUARDED_DISPATCH`. It happens to be suppressed by `Py_ReprEnter` first, so nothing is lost *here* — but the spelling is used tree-wide: `tupleobject.c:332`, `dictobject.c:3732`/`:3745`, `structseq.c:325`, `Python/context.c:988`/`:996`/`:1198`, `unionobject.c:304`. **`structseq.c` and `context.c` have zero `Py_ReprEnter`** (`grep -c`), so in those files the descent is invisible to *both* the vocabulary and the suppressor.

**Proposal A (vocabulary, 3 lines).** Add `PyUnicodeWriter_WriteRepr` to `_GUARDED_DISPATCH` with guard site `Objects/object.c:759 (via PyObject_Repr)` — the routing is `unicode_writer.c:394`, `PyObject *repr = PyObject_Repr(obj);`. Add `PyObject_Format` / `PyUnicode_FromFormat`-with-`%R` only if a later slice shows a need; I did not measure those.

**Proposal B (slot vocabulary).** Add `("_contains", "sq_contains")` to `_SLOT_NAME_SUFFIX`. `sq_contains` is a per-element descent slot by definition, and the corpus run already reaches `set_contains_key` through the *other* shape (`hash_entry_point`), which under-describes it.

### 4.2 [HIGH VALUE] The denominator field counts a different population from the findings

`recursion_prone_slot_functions` counts only `slot ∈ {tp_hash, parameter_walk}` (`:195`, `:1344`, `:1358`), while findings can be produced by four shapes, three of which do not require that slot. In this slice the number `1` and the finding `1` are unrelated. The envelope's own `slot_classification.note` warns about `from_slot_map` vs `from_name_suffix` but not about *this*, which is the more misleading of the two — the brief read the pair as a 1-of-1 precision measurement, which is exactly the reading the field invites.

**Proposal C.** Split the field and name what each covers:
```json
"denominators": {
  "hash_descent_candidates": 1,          // slot in {tp_hash, parameter_walk}
  "hash_descent_candidate_sites": [
    {"file":"Objects/bytesobject.c","line":1720,"function":"bytes_hash",
     "suppressed_by":"bound_zero_hash:Py_HashBuffer"}
  ],
  "dispatcher_descent_candidates": 13,   // slot-classified fns, any slot
  "self_recursion_candidates": 367,      // fns with a body, cycle-checked
  "type_tables_parsed": 0, "type_tables_seen": 8
}
```
The `suppressed_by` field is the part that matters: it turns "0 findings" into "0 findings, and here is the one candidate and the exact reason it was dropped", which is what a reviewer needs in order to certify a zero. `_BOUND_ZERO_HASH` already computes that reason and then discards it.

### 4.3 [HIGH VALUE] `Py_ReprEnter` discharges the guard obligation for the whole function — including hash descents it cannot bound

`_has_guard()` (`:357-359`) returns True on any of six tokens, `Py_ReprEnter` / `Py_ReprLeave` among them, and `_analyze_function` (`:799-800`) then returns `None` for the **entire function**. `Py_ReprEnter` bounds *cycles*, not *depth*, and bounds nothing at all for a `tp_hash` descent. A function that carries `Py_ReprEnter` and also calls `PyObject_Hash` would be silently suppressed.

**I checked whether that is live: it is not.** `scratchpad/reprenter_vs_hash2.py` over `Objects/` + `Modules/` + `Python/` using the scanner's own `extract_functions`: **19 functions contain `Py_ReprEnter`; none of them calls `PyObject_Hash` without a depth guard.** So this is a hardening, not a bug report — but it is one line to close and it is precisely the FP-taxonomy's *"guarded twins are twin for a specific threat model"* lesson expressed in the scanner.

**Proposal D.** Split `_GUARD_TOKENS` into `_CYCLE_GUARDS = {Py_ReprEnter, Py_ReprLeave}` and `_DEPTH_GUARDS = {Py_EnterRecursiveCall, …}`. A cycle guard discharges only the `guarded_dispatcher_descent` shape; **only** a depth guard (or `_has_depth_bound`) may discharge a `PyObject_Hash` descent or a self-recursion. Regression fixture: a synthetic `foo_repr` with `Py_ReprEnter` + `PyObject_Hash(self->key)`, asserted to fire.

### 4.4 [CRITICAL, corroborated, but NOT the cause here] the `bytesobject.c` truncation

Independently reproduced, and localised more precisely than the refcount agent's report:

- `extract_functions` returns **67** functions for `Objects/bytesobject.c`; the file has **3 926** lines and (by full-text brace balancing) **104** definitions. Last extracted: `bytes_getnewargs` @ **:2779**.
- Exact boundary: the `ERROR` cascade begins at **line 2787** and runs to :2820, followed by 8 more error/missing nodes through :2844 — the `bytes_methods[]` `PyMethodDef` table, where bare Argument-Clinic macro names (`BYTES_LSTRIP_METHODDEF`) sit adjacent with no separator. (The brief's §0 says 2784; measured, the last *extracted function* is at 2779 and the first ERROR node is at 2787. Four earlier ERROR nodes at :297/:303/:325/:340 are the `sprintf(buffer, "%lu", va_arg(...))` lines; those are local and do not cascade.)
- **1 147 lines / 37 functions invisible**, to this scanner and the eight others that share `extract_functions`.

**The live hypothesis I was handed — that the denominator of 1 is a truncation casualty — is disproven.** I enumerated every definition after :2779 (`bytes_mod`, `bytes_new_impl`, `_PyBytes_FromBuffer/List/Tuple/Iterator`, `PyBytes_FromObject`, `bytes_alloc`, `bytes_subtype_new`, `bytes_iteritem`, `PyBytes_Concat`, `PyBytes_ConcatAndDel`, `_PyBytes_Resize`, the six `striter_*`, `bytes_iter`, `_PyBytes_RepeatBuffer`, and the 20-function `PyBytesWriter_*` API). **None ends in `_hash`, `_repr`, `_str` or `_richcompare`**, so none would be slot-classified; and my full-text cycle sweep (§2.3) shows none participates in a call-graph cycle. Fixing the truncation would leave `recursion_prone_slot_functions` at **1** and `missing_recursion_guard` at **0**.

That is worth stating loudly for the toolkit's sake: the truncation is a severe cross-scanner defect (it hid Group A's reproduced `striter_next` FT crash), **and** it happens to cost this particular rule nothing. Both halves are true; conflating them would either understate the defect or overstate its reach.

**Proposal E** (seconding refcount-auditor §6.1, with one addition). Their canary — compare `functions_analyzed` against a cheap independent count and emit a `notes[]` entry on >20 % disagreement — is right. Add the *boundary*, not just the count: `{"file": "...", "tree_sitter_functions": 67, "text_scan_functions": 104, "first_error_node_line": 2787, "last_extracted_line": 2779}`. Without the boundary a reader knows coverage is partial but not *which half* was reviewed, and in this slice the answer to that ("everything after 2779") is what let me settle the hypothesis in ten minutes instead of re-reading the file.

### 4.5 Is the rule noisy here, or noisy everywhere? — corpus context, measured

I re-ran the scanner over all of `Objects/` (113 files, 4 861 functions): **61 findings — 54 `missing_recursion_guard`, 7 `guarded_by_dispatcher`; `recursion_prone_slot_functions: 33`.** The high-confidence numerator is the catalog: `tuple_hash` (CPY-0001), `frozendict_pair_hash` (CPY-0019), `ga_hash` (CPY-0020), `weakref_hash_lock_held` (CPY-0021), `union_hash`, `_Py_make_parameters` (CPY-0002), `_Py_subs_parameters` (CPY-0023), `merge_class_dict` (CPY-0071), `solid_base` (CPY-0097), `get_base_by_token_recursive` (CPY-0108), `update_subclasses` (CPY-0087).

**So the slice's 0/1 is a genuine local negative from a rule that is demonstrably firing correctly 200 lines away.** That is the strongest thing I can say about a zero, and it is only sayable because the corpus column exists in `RUN_CONTEXT.md`. Two out-of-slice rows I noticed and am recording in one line each, for whoever owns those files: **`Objects/memoryobject.c:3359 memory_hash`** (`field_element_descent`, high) and **`Objects/classobject.c:285 method_hash`** (`field_element_descent`, high) do not appear in the 171-record catalog excerpt in the briefing.

### 4.6 What worked

The informed-mode discipline paid once, precisely. The briefing's corrected FP entry — *"`PyObject_Hash` has NO recursion guard … never dismiss a hash-descent finding as dispatcher-guarded"* — is what made me check the *dispatcher* rather than the slot, which is what turned up R1 (`unsafe_object_compare`'s cached-slot call) as the one route in this slice that genuinely bypasses a guard. A cold run would have read `guarded_by: object.c:1099` and stopped.

The positive control was the other half. Fifteen consecutive rc=0 results are indistinguishable from a broken harness until `tuple_hash` segfaults on the same builds through the same subprocess wrapper. I would make that a standing requirement for this agent: **a recursion-guard report with no crash in it must carry a reproduced crash from a known member of the class, on the same builds.** It costs one file and forty seconds, and it is the difference between "clean" and "I did not measure anything".

---

# 5. What I did not do

- **No ASan/TSan run.** Neither adds anything to a stack-overflow verdict: the observable is the exit code, and `rc=-11` vs `rc=0` is unambiguous on plain builds. The one place ASan would have helped — locating the overflow frame — is moot because nothing overflowed in the slice.
- **No JIT builds.** `debug-gil-jit` / `release-gil-jit` were not exercised. The specialising interpreter reimplements list subscript/store/iteration inline (mapper §a), but **not** `COMPARE_OP` for lists — there is no `_COMPARE_OP_LIST` uop — so the JIT cannot change which dispatcher `list_richcompare` is reached through. That is a reading, not a measurement; a JIT run would settle it.
- **The `+1 unguarded frame` from R1/R2 is argued and measured at depth, not counted.** I did not instrument the exact frame delta between `a < b` and `[a,b].sort()`; the depth measurements at 10⁶ make the delta irrelevant, but if someone wants the number it is a gdb frame count away.
- **`_PyBytes_FormatEx` got the recursion pass only.** 480 lines, CC 159, rank 1 in all of `Objects/`, and inside the 1 147 invisible lines is its *caller* `bytes_mod`. Its recursion story is clean (no C self-recursion, every user hook costs a Python frame) but the rest of it is the refcount/memory agents' and remains, per Group A, unread line by line.
- **I did not file anything upstream.** Nothing here warrants it.

---

## Artifacts

Reproducers (`reports/obj-sequences/repro/`):
- `recursion_list_slot_matrix.py` — 27 scenarios over every list slot and comparison path; `RECUR_DEPTH` env var; per-scenario subprocess isolation
- `recursion_bytes_format_paths.py` — 20 scenarios over `_PyBytes_FormatEx`, `__contains__`, the `bytes(iterable)` constructors, and the two bytearray self-recursions, incl. the `contains_index_depth_counter` instrumentation
- `recursion_positive_control.py` — **the control**: `tuple_hash` and `ga_hash` at depth 400 000, rc=-11 on both GIL builds, plus the "slice types are not hash nodes" assertions

Scratch (`/tmp/…/scratchpad/`): `enumerate_slots.py` (reproduces `classified_total: 13`, `recursion_prone: 1` and names the function), `self_recursion_sweep.py`, `callgraph_cycles.py` (raw-text call graph, 339 nodes), `truncation_boundary.py` (the `:2787` ERROR node), `reprenter_vs_hash.py` / `reprenter_vs_hash2.py` (§4.3 tree-wide check).

## Summary line

```
FIX=0  CONSIDER=0  POLICY=1  ACCEPTABLE=(21 slot/path rows, each with a stated bound)
scan_recursion_guards.recursion_descent_guarded_by_dispatcher: 1 candidate / 1 correct / 0 dismissed
scan_recursion_guards.missing_recursion_guard: 0 findings on a denominator of 1 (bytes_hash) -- EARNED, reason = Py_HashBuffer, bound 0
1-of-1 DENOMINATOR VERDICT: the pairing is invalid -- the denominator counts bytes_hash (tp_hash),
    the finding is list_richcompare_impl (tp_richcompare). Hand count of recursion-capable slots: 13.
TRUNCATION VERDICT: bytesobject.c IS truncated at :2779 (1,147 lines / 37 fns invisible) but the
    denominator of 1 is NOT a casualty -- no *_hash/_repr/_str/_richcompare and no call-graph cycle
    exists in the invisible region.
positive control: tuple_hash + ga_hash @ depth 400k -> rc=-11 on release-gil-nojit AND debug-gil-nojit (4/4)
slice scenarios: 47 distinct (27 list + 20 bytes/bytearray); 47 x 2 GIL builds at depth 2e5,
    + full 27 re-run on release and 13 on debug at depth 1e6, + 7 cyclic x 2 FT builds
    under PYTHON_GIL=0  ==  148 runs, 148 x rc=0. Zero signals anywhere in the slice.
```
