# error-path-analyzer — Objects/ sample (informed)

**Owned shape:** `return-null-without-exception` (briefing §5) — a NULL return with no
`PyErr_Set*`, or an entry point that trusts `res != NULL` while `PyErr_Occurred()` is set.

## Scanner volume

```
raw candidates in sample: 29   |   confirmed: 0   |   dismissed: 29   |   precision: 0%
found by reading (scanner missed): 2 real (1 FIX reproduced live, 1 CONSIDER)
```

**Per-`type` precision — the number this run was asked to produce:**

| `type` | sample candidates | confirmed | precision | note |
|---|---|---|---|---|
| `unchecked_return` | 28 | 0 | **0%** | 5 distinct mechanical FP classes, all fixable |
| `sparse_error_cleanup` | 1 | 0 | **0%** | `confidence: low`; stated reason was factually wrong |
| `return_null_no_exception` | **0** | – | **n/a** | **the rule is structurally dead — see T1** |
| `missing_null_check` | 0 | – | n/a | 0 across all of `Objects/` too |
| `unchecked_parse` | 0 | – | n/a | Argument Clinic; expected 0 in `Objects/` |

My owned shape produced **zero candidates in the sample and only 5 across all 48k lines of
`Objects/`**. That is not because CPython is clean — it is a scanner defect I isolated and
quantified below (T1). This run's most useful output is therefore the toolkit assessment.

---

## Findings

### FIX

#### `Objects/unionobject.c:170-172` · `unionbuilder_add_single_unchecked` · **exception swallowed — reproduced live**

```c
Py_hash_t hash = PyObject_Hash(arg);
if (hash == -1) {
    PyErr_Clear();                 // <-- unconditional
    ...                            // treat arg as "unhashable", carry on
```

**What breaks.** `PyObject_Hash()` runs a *user-defined* `__hash__`. The author's intent is
"unhashable → `TypeError` → route to the `unhashable_args` list", but `PyErr_Clear()` discards
**whatever** exception was raised — `KeyboardInterrupt`, `MemoryError`, `RecursionError`, or any
user exception. The union is then built as if nothing happened.

**Guarded twin.** Every other `PyErr_Clear()` in the 14-file sample — **7 of 8** — first narrows
with `PyErr_ExceptionMatches`. This is the lone outlier:

| site | guard |
|---|---|
| `iterobject.c:75-78` | `PyErr_ExceptionMatches(PyExc_IndexError \|\| PyExc_StopIteration)` |
| `iterobject.c:247-248` | `PyExc_StopIteration` |
| `iterobject.c:403-404` | `PyExc_StopAsyncIteration` |
| `iterobject.c:427-433` | `PyExc_StopAsyncIteration` |
| `genericaliasobject.c:631-637` | `PyExc_AttributeError \|\| PyExc_TypeError` |
| `funcobject.c:1361-1362` | `PyExc_KeyError` |
| `descrobject.c:1907-1910` | `PyExc_AttributeError` |
| **`unionobject.c:171-172`** | **none** |

**How Python reaches it.** A metaclass `__hash__` — both the `|` operator and `typing.Union[...]`
funnel through this function.

```python
class MetaKI(type):
    def __hash__(cls): raise KeyboardInterrupt
class C(metaclass=MetaKI): pass
int | C          # -> int | __main__.C     (KeyboardInterrupt silently gone)

class MetaMem(type):
    def __hash__(cls): raise MemoryError
class D(metaclass=MetaMem): pass
typing.Union[int, D]   # -> int | __main__.D  (MemoryError silently gone)
```

**Verified**, not just read — `./python` @ `583cb03da83`:

```
--- int | C  (KeyboardInterrupt in __hash__) ---
NO EXCEPTION PROPAGATED -> result: int | __main__.C
--- typing.Union[int, D] (MemoryError in __hash__) ---
NO EXCEPTION PROPAGATED -> result: int | __main__.D
```

**Provenance.** Introduced by `dc6d66f44c0` (2025-03-04, gh-105499 "Merge typing.Union and
types.UnionType"). ~16 months live; affects both union spellings.

**Fix** (mirrors the 7 twins):

```c
if (hash == -1) {
    if (!PyErr_ExceptionMatches(PyExc_TypeError)) {
        return false;                  // propagate KeyboardInterrupt/MemoryError/...
    }
    PyErr_Clear();
```

**Classification: FIX.** Python-reachable, dynamically reproduced, swallows `KeyboardInterrupt`
and `MemoryError`, and 7 correctly-guarded siblings in the same sample establish the house rule.
Explicitly **not** the taxonomy's "`PyErr_Clear()` after a sentinel-returning lookup is idiomatic"
FP class: `PyObject_Hash` is not a sentinel-returning lookup, and its failure mode is
attacker-chosen, not a fixed `KeyError`/`AttributeError`.

**Sibling hunt, tree-wide** (`PyObject_Hash` → unconditional `PyErr_Clear`): exactly one other hit,
`Modules/_remote_debugging/binary_io_writer.c:290-293` `string_hash_func`. **Dismissed** — it is a
`_Py_hashtable` callback returning `Py_uhash_t` with no error channel, and its keys are internal
exact-`str` objects whose hash cannot fail. Signature-forced, not a bug.

---

### CONSIDER

#### `Objects/odictobject.c:1096-1099` · `_odict_popkey_hash` · `Py_NewRef()` on a nullable parameter

```c
if (_PyDict_Pop_KnownHash((PyDictObject *)od, key, hash, &value) == 0) {
    value = Py_NewRef(failobj);            // 1098  <-- failobj may be NULL
}
...
else if (value == NULL && !PyErr_Occurred()) {
    if (failobj) { value = Py_NewRef(failobj); }   // 1103  <-- guarded twin, 5 lines below
    else { PyErr_SetObject(PyExc_KeyError, key); }
}
```

**What breaks.** `failobj` is documented-nullable *by line 1103's own test*, and
`OrderedDict_popitem_impl` (`odictobject.c:1172`) passes `NULL` literally. `Py_NewRef(NULL)`
dereferences → SIGSEGV. The `_PyDict_Pop_KnownHash(...) == 0` branch means "node was in the
linked list but the key was not in the underlying dict" — the odict/dict duality desync (family
F6, the historically fragile one).

**Guarded twin:** lines 1103-1108, in the *same function*, five lines below.

**Reachability — honest negative.** I could not construct it from Python. Two attempts:
- `dict.__delitem__(od, k)` to bypass odict's `mp_ass_subscript`, then `popitem(last=False)` →
  `_odict_get_index` no longer finds the key, so `node == NULL` and the *guarded* branch runs.
  Result: clean `KeyError: 'a'`, no crash.
- A colliding-key `__eq__` that yanks the key mid-lookup → `_Py_dict_lookup`'s identity fast path
  skips `__eq__` for the exact key. Result: normal `(K(1), 'A')`, no crash.

**Classification: CONSIDER** — a latent NULL-deref with a one-line fix and an in-function guarded
twin, but no demonstrated Python-level path. The fix costs nothing:
`value = failobj ? Py_NewRef(failobj) : NULL;` with the `KeyError` set on the NULL leg.

---

### POLICY / ACCEPTABLE (one line each)

- `genericaliasobject.c:339-361` `_unpacked_tuple_args` — **POLICY**: undocumented sentinel-NULL convention (NULL means "absent", not "error"); correct *today* only because its sole caller disambiguates with `PyErr_Occurred()` at `:391`. This is my shape's **guarded twin** in this sample; a one-line comment on the function would lock the contract in.
- `interpolationobject.c:184-185` `_PyInterpolation_InitTypes` — **ACCEPTABLE**: `error: return _PyStatus_ERR(...)` leaves a live Python exception, but this is CPython house convention (`object.c:2678,2691,2697`, `floatobject.c:1860`, `longobject.c:6755`, `exceptions.c:4538` all do the same). No leak either: `tuple` is `Py_DECREF`'d before every `goto`, and `_PyType_GetDict` returns borrowed — so the scanner's stated reason is factually wrong.
- `genericaliasobject.c:583-588` `ga_getitem` — **CONSIDER, handed off to `ft-race-detector`**: lazy-init of `alias->parameters` with no `Py_BEGIN_CRITICAL_SECTION`, whereas `ga_parameters` (`:856-864`) got exactly that guard in `68abf17fa92` (gh-153298, 7 days before HEAD). A left-behind sibling of a week-old fix — but an FT race, not an error path; not mine to classify.
- **Retracted hypothesis** (recorded so nobody re-derives it): `genericaliasobject.c:783` `ga_reduce` — `Py_BuildValue("N(N)", _PyEval_GetBuiltin(&_Py_ID(next)), tmp)` looked like it leaks `tmp` when `_PyEval_GetBuiltin` returns NULL. It does not: `do_mktuple` sets `itemfailed` and *continues* the loop with `Py_None` placeholders, so the second `N` is still consumed and released. Measured: **+1 refcount over 1000 calls** (noise) on a debug build with a `next`-less `__builtins__`. Not a finding.

---

## New siblings of known shapes

The fix-propagation yield. One net-new instance, one scope escape, one hand-off:

| shape | new site | status |
|---|---|---|
| unconditional-`PyErr_Clear` (gh-146102 family) | `unionobject.c:172` | **FIX, reproduced live** |
| same, tree-wide sweep | `_remote_debugging/binary_io_writer.c:291` | dismissed (no error channel) |
| gh-153298 critical-section lazy-init | `genericaliasobject.c:583` | handed to ft agent |

The genericalias error-path cluster (Phase 1's #1 lead: 4 fixes in 5 months, two in the same
function) **did not yield a new error-path bug**. `_Py_make_parameters` and `_Py_subs_parameters`
are, on the error-path axis specifically, now correct: I walked all 14 `return NULL` sites in the
two functions and every one sets or propagates an exception, and every one releases `parameters` /
`item` / `tuple_args` / `newargs` correctly (the two apparent `Py_DECREF(parameters)` omissions at
`:243-247` are right, because `_PyTuple_Resize` already frees and NULLs on failure). The cluster is
real but the remaining unfixed sibling in that file is the **FT** one, not an error path. Reporting
that as a confident negative rather than manufacturing a finding.

---

## Classes bounded (clean negatives)

- **Missing `PyErr_NoMemory()` on OOM paths — CLEAN in the sample.** The briefing calls this "the
  usual offender" for my shape. Every raw (non-exception-setting) allocator in the 14 files:
  `capsule.c:236` `PyMem_Malloc` → `return PyErr_NoMemory();` at `:239`;
  `odictobject.c:704` `PyMem_Malloc` → `PyErr_NoMemory(); return -1;` at `:707`. That is the
  complete population — every other object allocation goes through `PyObject_GC_New` /
  `PyType_GenericAlloc` / `tp_alloc`, which set `MemoryError` themselves. **2/2 correct.**
- **`lazyimportobject.c` — CLEAN for this shape.** Phase 1's "newest type, zero bug-fix commits
  ever" lead. Read all 157 lines. Every `PyObject*` return either sets an exception
  (`_PyLazyImport_New:18,25`) or propagates one from a callee. `PyObject_GC_New` sets `MemoryError`
  itself. No sentinel-NULL, no raw allocator, no `PyErr_Clear`. The one scanner hit
  (`lazy_import_repr`) is an FP. Nothing to report — the file is small and genuinely careful.
- **`PyErr_Clear` discipline — 7/8 correct**, table above. The single outlier is the FIX.
- **`weakrefobject.c` — CLEAN for this shape** despite giving the most candidates (7) and having a
  53-commit fix history. All 7 are the same FP: `repr`/`res` is assigned and then *returned
  unmodified*, so the callee's exception propagates untouched. Its callback re-entrancy risk is
  real but belongs to `refcount-auditor`, not here.

---

## Toolkit assessment

### Precision — which rules produced FPs, and the FP class for each

0/29. All 28 `unchecked_return` FPs fall into **five** mechanical classes. Two are already in
`data/cpython_non_bugs.md`; **three are new and should be added**:

| FP class | n | why the rule fires | in taxonomy? |
|---|---|---|---|
| **A** — value is **returned directly**; callee's exception propagates | 13 | rule only looks for an explicit NULL *test* | **new** |
| **B** — checked in **positive form**: `if (v)`, `if (v != NULL)`, `while ((v = f()))`, `v == NULL ? :` | 8 | `_NULL_CHECK_RE_TEMPLATE` matches only `== NULL`, `!v`, `== 0` | **new** |
| **C** — checked via **`Py_SETREF` alias**: `Py_SETREF(item, tmp); if (item == NULL)` | 3 | check is on the *other* name | yes |
| **D** — **multi-assignment alias**: `args = tuple_args = PySequence_Tuple(args); if (args == NULL)` | 2 | regex captures only the innermost LHS | **new** |
| **F** — **struct-member LHS**: `ub->args = PyList_New(0); if (ub->args == NULL)` | 2 | `(\w+)` captures `args`, check reads `ub->args` | **new** |

Class A alone is 46% of the sample's noise. Classes A+B are 75%.

`sparse_error_cleanup` (1/1 FP, `confidence: low`) misfired for a reason worth noting: it scans for
`Py_DECREF` **after** the label, but the idiom in `_PyInterpolation_InitTypes` releases *before*
each `goto`, leaving nothing to release at the label. 6 hits across `Objects/`, 0 real.

### Recall gaps — the important part

**RG-1. `return_null_no_exception` is structurally dead — an off-by-one in `find_functions`.**
This is my owned rule, and it evaluates on ~1% of its eligible population.

`scan_error_paths.py:154-165`. After the `sig_start -= 1` adjustment that walks onto the return-type
line, the return type is still read from `lines[sig_start - 1]` — i.e. **the line above the return
type**. For CPython's canonical two-line signature that is a blank line or the previous function's
closing brace:

```c
                       // <- ret_type is read from HERE  (blank)
PyObject *             // <- sig_start lands here after the decrement
_PyLazyImport_New(...) // <- prev
{
```

Measured over all of `Objects/`:

| | current | patched |
|---|---|---|
| functions with **empty** `return_type` | **2351 / 2861 (82.2%)** | — |
| functions classified `PyObject`-returning | **22** | **1045** |
| ground truth (`^\s*(static\s+)?PyObject\s*\*\s*$`) | ~1645 | ~1645 |
| `return_null_no_exception` findings in `Objects/` | 5 | 734 |

2 of the 22 current hits aren't even return types — they're *comments* that happened to sit one line
above (`obmalloc.c:arena_map_is_used`, `dictobject.c:_PyDict_GetMethodStackRef`). The rule has been
reporting on ~20 functions out of 1645 since it was written.

**RG-2. No scanner in the toolkit can see the FIX I found.** `unionobject.c:172` is a
Python-reachable `KeyboardInterrupt`/`MemoryError` swallow. `scan_error_paths.py` has no
`PyErr_Clear` rule at all, and `scan_pyerr_clear.py` is scoped to the destructor family — which is
why it returns **0 across all of `Objects/`**. This is the live instance that settles the
RUN_CONTEXT open question, and Phase 1's answer ("scoping gap, not an upstream fix") is confirmed
with a concrete bug rather than an inference.

**RG-3. No rule models "`Py_NewRef`/`Py_INCREF` on a documented-nullable parameter."** The
`odictobject.c:1098` CONSIDER was found purely by reading. The tell is mechanical and cheap: the
same identifier is guarded by `if (param)` elsewhere in the *same function*.

### Prompt issues

- My agent definition is the generic C-extension error-path prompt. Its Output Format section
  conflicts with `RUN_CONTEXT.md`'s template (no **guarded twin**, no Python-reachability, no
  Toolkit assessment). I followed RUN_CONTEXT. The agent file should defer to the run template.
- The Classification Guide implies `unchecked_return` is normally actionable ("CONSIDER: unchecked
  return value where the result is not immediately dereferenced"). In CPython's own code it was
  **0/28**. The prompt should tell the agent to bulk-dismiss classes A–F by pattern and spend the
  budget on reading, which is where both real findings came from.
- The prompt says "Some APIs always succeed" but never mentions the far more common CPython case:
  APIs that *fail without setting an exception* (`_odict_clear_node` returning
  `PyErr_Occurred() ? -1 : 0`). That distinction is the core of my shape.

### Concrete tuning proposals (ranked by value)

**T1 — Fix the `find_functions` return-type off-by-one. `scan_error_paths.py:160-165`.**
Build the type from the signature lines themselves instead of the line above them:

```python
sig_text = " ".join(l.strip() for l in lines[sig_start:i])
idx = sig_text.find(func_name)
ret_type = sig_text[:idx].strip() if idx > 0 else ""
```

Verified: 22 → 1045 `PyObject`-returning functions; comment-as-return-type hits disappear.
**Must ship together with T2** — alone it takes `return_null_no_exception` from 5 to 734 in
`Objects/` (148 in the sample), all propagate-NULL noise. Landing T1 without T2 makes the toolkit
worse. Add a regression test asserting `return_type == "PyObject *"` for a two-line signature.

**T2 — Re-scope `return_null_no_exception` to the shape the briefing actually describes.**
The 500-char backward `PyErr_*` window can't distinguish "forgot to set an exception" from
"propagating the callee's". Replace with a narrow, precise rule — new `type`
**`alloc_null_no_memerror`**: emit only when a `return NULL` / `return -1` is guarded by a NULL test
of a variable assigned from a **non-exception-setting** allocator, and no `PyErr_NoMemory()` appears
between. Allocator set: `PyMem_Malloc`, `PyMem_Calloc`, `PyMem_Realloc`, `PyMem_RawMalloc`,
`PyMem_RawCalloc`, `PyObject_Malloc`, `PyObject_Realloc`, `malloc`, `calloc`, `realloc`.
Explicitly **exclude** `PyObject_New`/`PyObject_GC_New`/`PyType_GenericAlloc`/`tp_alloc`/`PyMem_New`
— they set `MemoryError` themselves; a taxonomy entry should say so. Measured on the sample: 2
candidate sites, both correct → **0 findings, 0 FPs**. A rule that is silent on clean code is worth
far more than one that emits 734.

**T3 — Add rule `unconditional_pyerr_clear` (recovers the only FIX in this run).**
Flag `PyErr_Clear()` **not** preceded within 3 lines by `PyErr_ExceptionMatches` /
`PyErr_GetRaisedException` / `PyErr_Fetch`. Measured on the sample: 8 sites → 7 suppressed, 1
emitted, and that one is the confirmed bug. **Precision 100% at n=1.** Put it in
`scan_error_paths.py` (whole-file scope), *not* in `scan_pyerr_clear.py`, whose destructor scoping
is deliberate and is exactly why it saw nothing.

**T4 — Kill the `unchecked_return` FP classes; this is the 0% → usable change.** All five are
regex-local:
- **A** (13): suppress if `return\s+{var}\s*;` occurs in the lookahead window.
- **B** (8): extend `_NULL_CHECK_RE_TEMPLATE` with `if\s*\(\s*{var}\s*\)`,
  `if\s*\(\s*{var}\s*!=\s*NULL\s*\)`, `while\s*\([^)]*{var}\s*=`, `{var}\s*==\s*NULL\s*\?`.
- **C** (3): on `Py_X?SETREF\(\s*(\w+)\s*,\s*{var}\s*\)`, re-target the NULL check to group 1.
- **D** (2): capture *all* LHS targets of `a = b = API(...)` and accept a check on any.
- **F** (2): widen the LHS capture to `(\w+(?:\s*(?:->|\.)\s*\w+)*)` and escape it in the check.

Applied to this sample these remove **28/28**. Ship with a fixture per class.

**T5 — Fix the reported line numbers; they are wrong on every finding in this run.** Two compounding
bugs:
1. Constant **+2** drift: `line = start_line + line_offset`, but `start_line` is the return-type
   line while `line_offset` is measured from the body (brace line + 1).
2. `strip_comments_and_strings` (`:67`) replaces `/*...*/` with a **single space**, destroying the
   newlines inside it, so every offset after a block comment under-counts by its line count.
   Fix: `re.sub(r'/\*.*?\*/', lambda m: '\n' * m.group(0).count('\n'), source, flags=re.DOTALL)`.

Combined effect measured: `_Py_subs_parameters` reported at 452, true call site **460** (drift 8 —
2 constant + 6 comment lines); `capsule.c` 255 vs 257; `ga_call` 645 vs 647. Every one of the 29
candidates pointed at the wrong line, which is pure triage tax.

**T6 — Retire or gate `sparse_error_cleanup`.** 6 hits in `Objects/`, 1 in the sample, 0 real, and
its stated reason was wrong. Either drop it or require that a locally-owned reference is still live
at the `goto` (i.e. not `Py_DECREF`'d on the line before).

**T7 — Four new entries for `data/cpython_non_bugs.md`, under "NULL checks / error paths":**
- *Value returned directly.* `res = PyUnicode_FromFormat(...); ... return res;` needs no NULL check —
  the callee's exception propagates. Not a bug.
- *Positive-form and loop-condition checks.* `if (v)`, `if (v != NULL)`,
  `while ((v = PyIter_Next(it)))`, `return v == NULL ? -1 : 0` are all checks.
- *Aliased assignment.* `a = b = API(...)` — the check may be on either name.
- *Struct-member destination.* `ub->args = PyList_New(0)` is checked as `if (ub->args == NULL)`.

And under a new "Allocators" heading: *`PyObject_New` / `PyObject_GC_New` / `PyType_GenericAlloc` /
`tp_alloc` / `PyMem_New` set `MemoryError` themselves* — only the raw `PyMem_*` / `malloc` family
needs an explicit `PyErr_NoMemory()`.
