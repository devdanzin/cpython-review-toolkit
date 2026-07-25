# pyerr-clear-auditor — Objects/ sample (informed)

**Mode: RECALL INVESTIGATION.** `scan_pyerr_clear` returned 0 on the 14-file sample and 0 across
all 50 files of `Objects/`. This report determines whether that zero is correct, and it is —
*for the class the scanner is scoped to*. The value of the run is in what the scoping misses.

## Scanner volume

```
raw candidates in sample: 0   |   confirmed: 0   |   dismissed: 0   |   precision: n/a
raw candidates in Objects/: 0
```

Precision is undefined (no candidates). Everything below is recall.

**Scanner is not broken.** Run tree-wide for control: `Modules/` yields 2 findings
(`_collectionsmodule.c:750 deque_clear` — the confirmed OOM-0039 — and
`_interpretersmodule.c:175 xibufferview_dealloc`). The detector fires when the pattern exists.

---

## Task 1 — the destructor-family zero: PROVEN, with a measured blind spot

Method: parsed all of `Objects/` with the toolkit's own `tree_sitter_utils.extract_functions` and
attributed every `PyErr_Clear` call-expression to its enclosing function and destructor slot.

| quantity | count |
|---|---|
| `grep -rn PyErr_Clear Objects/` raw lines | 91 |
| ├ bare `PyErr_Clear(` calls | **83** |
| ├ `_PyErr_Clear(tstate)` calls | 6 |
| ├ `_PyErr_ClearExcState(...)` | 1 |
| └ `#include` comment | 1 |
| bare clears attributed to a function by AST | 79 |
| bare clears the AST could not attribute (parse gaps) | 4 |
| **clears inside a destructor-family function** | **0** |
| destructor-family function *definitions* in `Objects/` | 226 |
| ├ seen by `extract_functions` | 218 |
| └ dropped (tree-sitter `ERROR` nodes) | 8 |
| dropped destructors that contain a `PyErr_Clear` | **0** |

The 8 dropped destructors — `bytesobject.c:3418 striter_dealloc`, `:3427 striter_traverse`,
`dictobject.c:5658 dictiter_dealloc`, `:5669 dictiter_traverse`, `:6433 dictview_dealloc`,
`:6443 dictview_traverse`, `object.c:2286 none_dealloc`, `:2432 notimplemented_dealloc` — were each
read directly. None contains a clear. **The zero survives the blind spot.**

`Objects/` genuinely has no unguarded `PyErr_Clear()` in any `tp_dealloc` / `tp_clear` /
`tp_finalize` / `tp_traverse`. This is a real clean negative and should be recorded as one.

### `OOM-0023 Objects/typeobject.c subtype_dealloc` — verdict: **MIS-CATALOGUED (stale entry)**

`subtype_dealloc` is at `Objects/typeobject.c:2719-2916` on main @ `4f3be1b5777`. Read in full.
**It contains no `PyErr_Clear()`, and no `PyErr_*` call of any kind.** Its teardown is
`PyObject_CallFinalizerFromDealloc` / `PyObject_ClearWeakRefs` / `clear_slots` / `basedealloc` —
every one of which manages exception state internally.

Nor is this a recent upstream fix: `git log -S PyErr_Clear -- Objects/typeobject.c` shows the last
touch anywhere near this code is `41c57b33533` (bpo-37994, **2019**), and it is in the attribute
lookup path, not in `subtype_dealloc`. There is no commit removing a clear from `subtype_dealloc`.

**Conclusion: neither an upstream fix nor a scanner miss — the catalog entry is wrong.**
The other two exemplars for this shape are sound: `deque_clear` reproduces (scanner finds it today),
and `context_tp_dealloc` (gh-152083) is in `Modules/_contextvarsmodule.c`, outside this scope.

Action: drop `OOM-0023 subtype_dealloc` from `data/cpython_known_bugs.tsv:30` and from the
`confirmed_examples` list in `data/cpython_bug_shapes.json:24`, or re-point it at the function that
actually carried the bug. Leaving it in place makes every future run re-litigate a phantom and
falsely suggests the scanner has a recall hole in `typeobject.c`.

### Whole-function suppression caveat — checked, currently harmless

`_has_save_restore_guard()` tests `any(api in func["body"])` — whole-function, not positional. In
`Objects/` it suppressed **nothing** (0 clears in destructors, so it never fired). Tree-wide only
two functions trip it, and both bracket correctly:

- `Modules/_io/iobase.c:296 iobase_finalize` — `PyErr_GetRaisedException()` at :302, three clears at
  :307/:314/:320, `PyErr_SetRaisedException(exc)` at :332. All three are inside the bracket. Correct.
- `Modules/_sqlite/connection.c:471 connection_finalize` — save :473, clear :495, restore :503. Correct.

So the caveat is real as a *design* weakness but currently costs zero findings. Hardening it is
therefore free (see proposal T6).

---

## Task 2 — the success-path class in the 14-file sample

All 8 `PyErr_Clear()` sites in the sample, every one of them outside the destructor family.
Phase 1 predicted "roughly 8 sites left in the sample" — exact.

| # | site | function | narrowing guard | verdict |
|---|---|---|---|---|
| 1 | `unionobject.c:172` | `unionbuilder_add_single_unchecked` | **none** | **CONSIDER (real)** |
| 2 | `iterobject.c:248` | `calliter_iternext` | `StopIteration`, but reachable with no exception set | **CONSIDER (real)** |
| 3 | `genericaliasobject.c:637` | `set_orig_class` | `AttributeError`\|`TypeError` | ACCEPTABLE |
| 4 | `descrobject.c:1910` | property `__doc__` setter | `AttributeError` + issue-linked comment | ACCEPTABLE |
| 5 | `funcobject.c:1362` | `descriptor_set_wrapped_attribute` | `KeyError`, then immediate `PyErr_Format` re-raise | ACCEPTABLE |
| 6 | `iterobject.c:78` | `iter_iternext` | `IndexError`\|`StopIteration` | ACCEPTABLE |
| 7 | `iterobject.c:404` | `anextawaitable_iternext` | `StopAsyncIteration` | ACCEPTABLE |
| 8 | `iterobject.c:433` | `anextawaitable_proxy` | `StopAsyncIteration` | ACCEPTABLE |

Also checked and clean: **no dropped fallible results** in the sample. A sweep for bare-statement
calls to `PyObject_SetAttr` / `PyObject_SetItem` / `PyDict_SetItem` / `PyDict_DelItem` /
`PyList_Append` / `PyObject_GenericSetAttr` and their `(void)`-cast forms across all 14 files
returns nothing. Every fallible int-returning call in the sample has its result tested.

### FIX / CONSIDER

#### [CONSIDER] `Objects/unionobject.c:172` · `unionbuilder_add_single_unchecked` · unfiltered clear swallows any `__hash__` exception

```c
static bool
unionbuilder_add_single_unchecked(unionbuilder *ub, PyObject *arg)
{
    Py_hash_t hash = PyObject_Hash(arg);
    if (hash == -1) {
        PyErr_Clear();            /* <-- clears WHATEVER __hash__ raised */
        if (ub->unhashable_args == NULL) { ... }
```

**What breaks.** `PyObject_Hash(arg)` dispatches to `type(arg).__hash__`, i.e. arbitrary Python.
The `-1` return only means *something* was raised. The code assumes "unhashable ⇒ TypeError" and
clears unconditionally, so a `MemoryError`, `KeyboardInterrupt`, `RecursionError` or
`SystemExit` raised inside a user `__hash__` is discarded and `arg` is silently reclassified as
an unhashable union member. The union is then built successfully — a swallowed interrupt becomes a
silent success, which is precisely the harm the destructor class is scoped to catch.

**Guarded twin.** `Objects/genericaliasobject.c:637 set_orig_class` — same family (the F1
parameter-walk / typing group), same "recover from an expected failure" intent, but it narrows first:

```c
if (!PyErr_ExceptionMatches(PyExc_AttributeError) &&
    !PyErr_ExceptionMatches(PyExc_TypeError))
{
    Py_DECREF(obj);
    return NULL;              /* propagate anything unexpected */
}
PyErr_Clear();
```

The fix is to add the identical `if (!PyErr_ExceptionMatches(PyExc_TypeError)) return false;` before
the clear.

**How Python reaches it.** `unionbuilder_add_single_unchecked` ← `unionbuilder_add_single:227`
(unchecked path) ← `_Py_union_type_or:269` = the `|` operator on types:

```python
class Meta(type):
    def __hash__(cls): raise KeyboardInterrupt
class C(metaclass=Meta): pass
int | C          # KeyboardInterrupt silently swallowed; a union is returned
```

Also reachable via `union_nb_or:404` (`is_checked=true`, `typing.Union[...]`) and
`unionbuilder_add_tuple:236`.

**Classification.** CONSIDER, not FIX: the outcome is a swallowed exception and a wrong-but-valid
object, not memory unsafety, and no differential has been run. It is the strongest single site in
the sample and the one worth filing.

**Not the taxonomy FP class.** The taxonomy exempts "`PyErr_Clear()` after a sentinel-returning
lookup (`PyObject_GetAttr` / `PyDict_GetItemWithError` / `PyMapping_GetOptionalItem`)". `PyObject_Hash`
is not a sentinel-returning lookup — it has no "missing key" outcome, and its `-1` carries an
arbitrary user exception. The seven other sample sites *are* in that FP class or narrow explicitly,
and are dismissed above without further comment per informed-mode rule 2.

#### [CONSIDER] `Objects/iterobject.c:248` · `calliter_iternext` · exception test on a success path

```c
result = _PyObject_CallNoArgs(it->it_callable);
if (result != NULL && it->it_sentinel != NULL){        /* 234 */
    ...
}
else if (PyErr_ExceptionMatches(PyExc_StopIteration)) { /* 247 */
    PyErr_Clear();                                      /* 248 */
    Py_CLEAR(it->it_callable);
    Py_CLEAR(it->it_sentinel);
}
Py_XDECREF(result);
return NULL;
```

**What breaks.** The `else if` is taken when `result == NULL` **or** `it->it_sentinel == NULL`. The
second disjunct is a *success* path — `result` is a valid object and no exception is set. The guard
at :229 (`it_callable == NULL → return NULL`) does not cover it, because the re-entrant case clears
`it_sentinel` while the outer frame is inside the call at :233: a callable passed to
`iter(callable, sentinel)` that itself advances the same iterator to exhaustion returns to :234 with
`result != NULL, it_sentinel == NULL`. `PyErr_ExceptionMatches` is then consulted with no exception
in flight; if the caller has a stale `StopIteration` pending, `PyErr_Clear()` discards it. On every
such path the valid `result` is also dropped via `Py_XDECREF` and NULL returned.

**Guarded twin.** `Objects/iterobject.c:78 iter_iternext` — same file, same protocol — tests the
result first (`if (result != NULL) { ...; return result; }`) and only *then* inspects exception
state, so its `PyErr_Clear()` is unreachable without an exception. The fix is the same restructure:
handle `result == NULL` in its own branch rather than as the `else` of a compound condition.

**Classification.** CONSIDER. Reaching it needs re-entrancy plus a stale pending exception; the
more likely observable harm is the dropped `result`, which is a correctness bug adjacent to this
class rather than in it.

### POLICY / ACCEPTABLE (one line each)

- `genericaliasobject.c:637` `set_orig_class` — narrows to `AttributeError`/`TypeError` before clearing; this is the guarded twin, not a finding.
- `descrobject.c:1910` property `__doc__` — narrows to `AttributeError`, gated on `!self->getter_doc`, with an in-code link to gh-98963 documenting the deliberate 3.11 back-compat behaviour.
- `funcobject.c:1362` `descriptor_set_wrapped_attribute` — narrows to `KeyError` and immediately re-raises as `AttributeError`; clear-then-reraise is the correct idiom.
- `iterobject.c:78` `iter_iternext` — narrows to `IndexError`/`StopIteration`, and the result is tested before exception state is touched.
- `iterobject.c:404` / `:433` `anextawaitable_*` — narrow to `StopAsyncIteration` and immediately substitute `StopIteration(default)`; this is the documented `anext(it, default)` contract.

---

## Task 3 — the recall gap: **9 real sites in `Objects/`, plus 1 CONSIDER**

Every one of the 79 attributed `PyErr_Clear` calls in `Objects/` is outside the destructor family,
so the destructor-only scoping misses **100% of the class by construction**. Triaging those 79:

```
79 attributed clears
├─ 43  narrowed by PyErr_ExceptionMatches           → the taxonomy's idiomatic FP class, correctly ignored
└─ 36  unfiltered
   ├─  1  Class A — clear on a provable success path                    (gh-146102 shape proper)
   ├─  8  Class B — unfiltered clear of an arbitrary user-callback exception
   └─ 27  bounded / documented-contract                                 → POLICY, correctly ignored
```

### Class A — success-path clear (1 site)

**`Objects/memoryobject.c:3262` · `memory_richcompare`** — the only textbook gh-146102 instance in
`Objects/`.

```c
if (!equiv_shape(vv, ww)) {
    PyErr_Clear();          /* 3262 — nothing can be set here */
    equal = 0;
    goto result;
}
```

`equiv_shape` (`memoryobject.c:308`) compares `Py_buffer->ndim` and `->shape[]` integers and returns
0/1. It calls nothing and sets no exception. Reaching :3261 requires that both buffer acquisitions
above it succeeded (:3251 or :3254-3258), so no exception is set by this function on this path. The
clear can therefore only discard a **caller-owned** pending exception. Dates to `06e1ab0a6b5` (2012).

**Guarded twin: `Objects/odictobject.c:2225`** — Victor Stinner's own fix `b3b7c0025be`
(gh-146102, 2026-06-15), in a sample file:

```c
 static int
 mutablemapping_add_pairs(PyObject *self, PyObject *pairs)
 {
+    assert(!PyErr_Occurred());
+
     ...
     if (iterator == NULL)
         return -1;
-    PyErr_Clear();
```

The prescribed fix is mechanical: delete the clear, add `assert(!PyErr_Occurred())`. That idiom is
already established in `Objects/` — 20 occurrences across 14 files, including 4 of the 14 sample
files (`weakrefobject.c:1094`, `descrobject.c:269`, `funcobject.c:1232`, `odictobject.c:2225`).

### Class B — unfiltered clear of an arbitrary user-callback exception (8 sites)

Each clears whatever a user-defined `__hash__` / `__buffer__` / `__index__` / import-time code
raised, discarding `MemoryError` / `KeyboardInterrupt` / `RecursionError` along with the expected
`TypeError`. Same harm as the destructor class, different location.

| site | function | Python-reaching call | user hook |
|---|---|---|---|
| `unionobject.c:172` | `unionbuilder_add_single_unchecked` | `PyObject_Hash` | `__hash__` (**in sample**) |
| `abstract.c:350` | `PyObject_CheckReadBuffer` | `PyObject_GetBuffer` | `__buffer__` (PEP 688) |
| `bytearrayobject.c:1171` | `bytearray_richcompare` | `PyObject_GetBuffer` | `__buffer__` |
| `bytearrayobject.c:1177` | `bytearray_richcompare` | `PyObject_GetBuffer` | `__buffer__` |
| `memoryobject.c:3255` | `memory_richcompare` | `PyObject_GetBuffer` | `__buffer__` |
| `bytes_methods.c:608` | `_Py_bytes_contains` | `PyNumber_AsSsize_t` | `__index__` |
| `codeobject.c:3217` | `hash_const` | `PyObject_Hash` | `__hash__` via `types.CodeType` |
| `moduleobject.c:1319` | `try_load_lazy_submodule` | `_PyImport_TryLoadLazySubmodule` | arbitrary module-level code |

Two carry mitigating documentation and would likely be re-classified POLICY on triage:
`codeobject.c:3217` has an explicit gh-130851 comment justifying the address fallback, and
`moduleobject.c:1319`'s header comment declares "Returns a new reference on success, or NULL with no
error set". `moduleobject.c:1319` is nonetheless the highest-impact of the eight — it swallows a
`KeyboardInterrupt` raised anywhere inside a lazy submodule import (F8 family, the newest code in the
tree), converting it into a plain `AttributeError`.

The four `PyObject_GetBuffer` sites share one guarded twin idiom: test `PyObject_CheckBuffer(obj)`
(a type-flag test that raises nothing) *before* calling `PyObject_GetBuffer`, so the clear is
never needed. That is a single-shape sweep across three files.

### The 27 correctly-ignored POLICY sites

Recorded so a future widening does not re-surface them as noise: bounded numeric conversions
(`rangeobject.c:1240/1245/1250/1304/1309/1314` and `enumobject.c:65` — only `OverflowError` is
possible from `PyLong_AsLong` on a known `PyLong`); documented never-fails contracts
(`typeobject.c:6149/6158/6183 find_name_in_mro`, whose header comment states "It never set an
exception"; `typeobject.c:9748/9786`; `unicodeobject.c:14695/14717/14730 intern_common`);
interpreter-init status conversion (`unicodeobject.c:342/14499` → `_PyStatus_ERR`); debug-only
output (`object.c:2943`, `moduleobject.c:1071/1093` verbose-mode tracing);
and API-contract-cannot-report (`object.c:1617` `_PyObject_GetDictPtr` returns `PyObject**`).

---

## New siblings of known shapes

- **gh-146102 (`PyErr_Clear` on success) — 1 new sibling:** `memoryobject.c:3262`. The upstream sweep
  is 2 of N commits; this is an N+1 candidate, with the fix already demonstrated in `odictobject.c`.
- **`PyObject_GetBuffer`-then-clear — 4 sites, one shape** (`abstract.c:350`,
  `bytearrayobject.c:1171/1177`, `memoryobject.c:3255`). Not previously catalogued.
- **`gh-146102` guarded-twin density measured:** `assert(!PyErr_Occurred())` appears 20× in 14
  `Objects/` files. Useful as a *positive* signal — files that already use it are the ones whose
  maintainers accept the idiom.

## Classes bounded (clean negatives)

- **`PyErr_Clear()` in the destructor family, all 50 files of `Objects/`: none.** 226 destructor
  definitions, 218 AST-visible + 8 read by hand. Evidence in Task 1.
- **Dropped fallible results in the 14-file sample: none.** Sweep described in Task 2.
- **Whole-function suppression is not hiding anything today**, in `Objects/` or tree-wide. Evidence
  in Task 1.

## Scope escapes

- `Modules/_contextvarsmodule.c context_tp_dealloc` (gh-152083) and
  `Modules/_collectionsmodule.c deque_clear` (OOM-0039) are the shape's live exemplars and sit
  outside `Objects/`. The scanner finds `deque_clear` today.
- The 70 `_PyErr_Clear(` call sites in `Python/` are invisible to the scanner for the reason in T1
  below; they were not triaged here.

---

## Toolkit assessment  ← MAIN BODY

### Precision

Not measurable — zero candidates. No rule produced a false positive because no rule produced
anything. The *scoping* is high-precision by design and that is confirmed: had the scanner been
naively widened to all `PyErr_Clear` in `Objects/`, it would have emitted **79 candidates of which
~9 are real — 89% FP**, dominated by exactly the sentinel-lookup class the taxonomy warns about.
The current narrow scope is the right default; the proposals below widen it without paying that cost.

### Recall gaps

**R1 — the name filter misses `_PyErr_Clear(tstate)`, and this costs a finding in the scanner's own class.**
`_find_pyerr_clear_calls` compares the callee text to the literal `"PyErr_Clear"`. CPython internals
overwhelmingly use the tstate-passing `_PyErr_Clear(tstate)` form: **70 call sites in `Python/`,
6 in `Objects/`, 2 in `Modules/`** — all invisible. One of them is a live miss of the *destructor*
class the scanner already claims:

```c
/* Python/pystate.c:819  interpreter_clear()  — matches the scanner's own "_clear" suffix rule */
if (_PySys_Audit(tstate, "cpython.PyInterpreterState_Clear", NULL) < 0) {
    _PyErr_Clear(tstate);          /* :836 — no save/restore anywhere in the function */
}
```

An audit hook that raises during interpreter teardown is silently dropped. (Severity is arguable —
this is interpreter finalization, not object teardown — but it is unambiguously inside the scanner's
declared scope and unambiguously missed.) **This is the single cheapest, highest-yield fix.**

**R2 — the scoping predicate itself is the main gap** (already established by Phase 1, now
quantified): destructor-only scoping misses 100% of `Objects/` clears, of which 9 are real. See
Task 3. gh-146102 established the success-path variant as a bug class on 2026-06-15; the scanner
predates that and has not been re-scoped.

**R3 — `extract_functions` silently drops functions in files with tree-sitter `ERROR` nodes.**
Measured across `Objects/`: `dictobject.c` 181 of ~290 definitions recovered (17 ERROR nodes),
`object.c` 65 of ~140 (15 ERROR nodes), `typeobject.c` 74 ERROR nodes, `weakrefobject.c` 42.
For the destructor family specifically: **8 of 226 definitions (3.5%) are invisible**, listed in
Task 1. Here it happens to cost nothing — none of the 8 contains a clear — but a 3.5% blind spot
means the scanner *cannot presently prove* a zero, and this run had to prove it by hand.
This is the same class of chassis defect as the `extract_functions`-skips-`preproc_ifdef` gap
confirmed in the multidict review; it affects **every** scanner built on this chassis, not just
this one. Four bare clears (`bytesobject.c:2940`, `dictobject.c:7269`, `typeobject.c:10931`,
`typeobject.c:11032`) could likewise not be attributed to any function.

**R4 — the destructor-name convention misses CamelCase teardown functions.**
`_destructor_kind` matches the snake_case suffixes `_dealloc`/`_finalize`/`_clear`/`_traverse`.
`Objects/moduleobject.c:1038 _PyModule_Clear` and `:1046 _PyModule_ClearDict` are real teardown
functions — called from `Python/pylifecycle.c:1874/1887/1893` during interpreter finalization, and
containing two `PyErr_Clear()` calls — and neither matches. Low finding-yield here (both clears are
verbose-mode tracing, ACCEPTABLE) but the hole is structural.

**R5 — the suppression is not positional.** Design weakness only; measured cost today is zero
(Task 1). Worth fixing while it is free rather than after it costs a finding.

### Prompt issues

**P1 — the agent definition asserts a bug that does not exist.** Both the `description`
frontmatter and the "Why this matters" section name `subtype_dealloc` as a "confirmed instance",
and Phase 2 of the Analysis Strategy uses it as the worked example of whole-function suppression
hiding a second clear. `subtype_dealloc` contains no `PyErr_Clear` and the git history shows it
never lost one. This actively misdirected the run: the natural first move on a zero is to look for
the scanner miss in `subtype_dealloc`, and that time was spent proving a negative. Remove it from
the prompt in the same edit as the data-file entries.

**P2 — the prompt has no instruction for the zero-candidate case.** It is written entirely as a
triage prompt ("For each finding, ask whether..."). On a scope where the class is clean the agent
has no prescribed next move. `RUN_CONTEXT.md` supplied that instruction externally; it belongs in
the agent definition so it applies outside informed runs too.

**P3 — the prompt's scope statement and its bug-class statement have diverged.** The prompt says
"a `PyErr_Clear()` there... silently swallows the caller's live MemoryError". That harm model is not
destructor-specific — it applies verbatim to `unionobject.c:172`. The prompt should state that the
destructor restriction is a *precision* choice, not a statement about where the harm occurs, so a
reader knows the scanner's silence is not a claim of cleanliness.

### Concrete tuning proposals (ranked by value)

**T1 — match `_PyErr_Clear` as well as `PyErr_Clear`.** One-line change, immediate finding.
```python
# scan_pyerr_clear.py, _find_pyerr_clear_calls
_CLEAR_NAMES = frozenset({"PyErr_Clear", "_PyErr_Clear"})
...
if fn_node and get_node_text(fn_node, source_bytes) in _CLEAR_NAMES:
```
*Yield:* +1 finding immediately (`Python/pystate.c:836 interpreter_clear`), +78 call sites made
visible tree-wide for T2/T3. *FP cost:* zero — `_PyErr_Clear(tstate)` is semantically identical.

**T2 — new type `pyerr_clear_on_success_path` (the gh-146102 class).** Highest precision of the
widening rules; enable outside the destructor family.

> Walk from the `PyErr_Clear()` call up to the enclosing function body, collecting the condition
> expression of every enclosing `if` / `else if` / `while`. Flag when **none** of those conditions
> references an error signal — i.e. none contains `PyErr_Occurred`, `_PyErr_Occurred`,
> `PyErr_ExceptionMatches`, `_PyErr_ExceptionMatches`, `DKIX_ERROR`, nor a comparison of an
> identifier against `NULL` / `0` / `-1` where that identifier is assigned from a call earlier in
> the same block. A clear at statement level with **no** enclosing conditional at all is the
> strongest sub-case and should be `confidence: high` — that is the exact shape of the odict fix.

*Yield in `Objects/`:* 2 (`memoryobject.c:3262`, `iterobject.c:248`). *Estimated FP:* low, ~0-30% —
the rule is a dominance test, not a heuristic. *Recommended fix text:* "delete the clear, add
`assert(!PyErr_Occurred())`", citing `Objects/odictobject.c:2225`.

**T3 — new type `pyerr_clear_unfiltered_after_python_call`.** Medium precision; the volume rule.

> Flag a `PyErr_Clear()` when (a) the fallible call whose failure the enclosing branch tests is in
> a **Python-reaching** set, AND (b) no `PyErr_ExceptionMatches` / `PyErr_GivenExceptionMatches`
> appears between the failure test and the clear.
>
> Suppress when: the clear is immediately followed by `PyErr_Set*` / `PyErr_Format*`
> (clear-then-reraise); the enclosing function returns `PyStatus`; the failing call is a
> non-Python-reaching numeric conversion.
>
> New `data/cpython_python_reaching_calls.json` seed: `PyObject_Hash`, `PyObject_GetBuffer`,
> `PyObject_RichCompare`, `PyObject_RichCompareBool`, `PyObject_Call`, `PyObject_CallNoArgs`,
> `_PyObject_CallNoArgs`, `PyObject_CallMethod*`, `PyObject_GetAttr`, `PyObject_SetAttr`,
> `PyObject_GetItem`, `PyObject_SetItem`, `PyNumber_Index`, `PyNumber_AsSsize_t`,
> `PyObject_Str`, `PyObject_Repr`, `PyObject_IsTrue`, `PyIter_Next`, `PyType_Ready`,
> `_PyImport_TryLoadLazySubmodule`.

Condition (b) is what makes this affordable: it alone removes 43 of the 79 `Objects/` sites — the
sentinel-lookup FP class the taxonomy names. Condition (a) removes a further ~25.
*Yield in `Objects/`:* ~11 raw candidates, 8 judged real → **estimated FP ~30%**, in line with the
other scanners in this toolkit. Emit at `confidence: medium` and default the class to CONSIDER,
not FIX — several true positives are deliberate-and-documented and belong in POLICY after triage.

**T4 — chassis: `extract_functions` must not silently drop functions.** Highest cross-cutting value;
fix in `tree_sitter_utils.py`, benefiting all 23 scanners.
- When a parse yields `ERROR`/`MISSING` nodes, supplement the AST sweep with a regex definition
  recovery (`^([A-Za-z_]\w*)\s*\(` at column 0 followed by a `{` within 6 lines) and brace-match the
  body, so no function is invisible.
- Add `parse_errors: N` and `functions_recovered_by_fallback: N` to the JSON envelope, so a reported
  zero is auditable rather than taken on trust. Had this field existed, this run would have started
  from evidence instead of a hand re-derivation.

**T5 — widen the destructor-name convention.** In `_DESTRUCTOR_SUFFIXES`, add case-insensitive
CamelCase forms (`Clear`, `ClearDict`, `Dealloc`, `Finalize`, `Traverse`) or add
`_PyModule_Clear` / `_PyModule_ClearDict` to `_DESTRUCTOR_EXACT`. *Yield:* 2 new in-scope clears in
`Objects/` (both ACCEPTABLE on triage), closes the structural hole. Low priority.

**T6 — make the save/restore suppression positional.** Replace whole-function
`_has_save_restore_guard` with: a clear at line L is guarded iff a save API appears at line `< L`
**and** a restore/report API appears at line `> L`, both within the function body. *Measured cost:
zero* — both tree-wide suppressed functions (`iobase_finalize`, `connection_finalize`) still pass.
Do it now while it is free.

**T7 — data-file corrections** (see Task 1 for evidence):
- `data/cpython_known_bugs.tsv:30` — delete or re-point the `OOM-0023 subtype_dealloc` row.
- `data/cpython_bug_shapes.json:24` — drop `"OOM-0023 subtype_dealloc"` from `confirmed_examples`;
  optionally add `"gh-146102 mutablemapping_add_pairs"` as the success-path exemplar.
- `commands/known-issues.md:61` — the `OOM-0023 | likely_fixed | ... | read the file to confirm`
  row is now resolved: mark it `invalid`.
- `agents/pyerr-clear-auditor.md` — remove `subtype_dealloc` from the description and from the
  Phase 2 example (P1), and add a zero-candidate instruction (P2).
- `data/cpython_non_bugs.md` — the "PyErr_Clear after a sentinel-returning lookup is idiomatic"
  entry is **correct and load-bearing**; it predicted the 43/79 split exactly. Extend it with the
  measured counterexample: *"...but an unfiltered clear after a call that runs arbitrary Python
  (`PyObject_Hash`, `PyObject_GetBuffer`, `PyNumber_AsSsize_t`) is not in this class — it discards
  MemoryError/KeyboardInterrupt along with the expected TypeError. Require an `ExceptionMatches`
  narrowing."*

**Suggested sequencing.** T1 + T7 are minutes of work and settle two standing questions. T4 is the
highest-value item in the list because it is chassis-wide. T2 before T3 — T2 is the class upstream
is actively sweeping and has near-zero FP cost, so it earns the scanner credibility that T3's 30%
will spend.
