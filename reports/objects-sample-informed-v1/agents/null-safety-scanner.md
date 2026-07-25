# null-safety-scanner — Objects/ sample (informed)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scanner:** `scan_null_checks.py` · pre-filtered `scanners/scan_null_checks.sample.json`
**Differential binary:** `./python` @ `583cb03da83` (ASan+debug, an ancestor of HEAD; the
code under test is byte-identical to HEAD in both crash sites).

## Scanner volume

```
raw candidates in sample: 21   |   confirmed: 0   |   dismissed: 21   |   precision: 0%
```

**All 21 scanner candidates are false positives.** The one real NULL dereference in the
sample — and the second one confirmed by differential — were both found **by reading and by
OOM injection**, not by the scanner. This is a recall report, not a precision report.

| verdict | count |
|---|---|
| FIX (found by reading + reproduced) | **1** |
| Cross-confirm of a catalog entry (reproduced live at HEAD) | 1 |
| CONSIDER | 1 |
| ACCEPTABLE (all 21 scanner candidates) | 21 |

---

## Findings

### FIX

#### `Objects/genericaliasobject.c:302` · `subs_tvars` · `Py_DECREF(NULL)` → SIGSEGV

**This is the lead from PHASE1_DIGEST §"Top fix-propagation leads" #2, and it is worse than
described: the refleak sweep did not walk past the defect — it *introduced* it, and the
NULL-deref sweep two months later fixed the identical shape 60 lines above and left this one.**

```c
                if (Py_TYPE(param)->tp_iter && PyTuple_Check(arg)) {  // TypeVarTuple
                    j = tuple_extend(&subargs, j,
                                    &PyTuple_GET_ITEM(arg, 0),
                                    PyTuple_GET_SIZE(arg));
                    if (j < 0) {
                        Py_DECREF(subparams);
                        Py_DECREF(subargs);     // <-- subargs is ALWAYS NULL here
                        return NULL;
                    }
```

**What breaks.** `tuple_extend()` (`genericaliasobject.c:169`) returns `-1` only when
`_PyTuple_Resize(dst, …)` failed. `_PyTuple_Resize` (`tupleobject.c:1036`) NULLs its out-param
on **every** failure path — `*pv = 0` in the `PyErr_BadInternalCall` branch, `*pv = NULL` in the
`PyObject_GC_Resize` branch, and `*pv = PyTuple_New(newsize)` (NULL on failure) in the
`oldsize == 0` branch. So `j < 0` ⟹ `subargs == NULL`, and `Py_DECREF(subargs)` reads
`op->ob_refcnt_full` at address 0.

**Provenance (the fix-propagation story).**

| commit | date | what it did to this shape |
|---|---|---|
| `8060aa5d7dd` gh-145376 "Fix various refleaks in Objects/" | 2026-03-09 | **added** `Py_DECREF(subargs)` at this site (over-correcting a real `subparams` leak) |
| `634568d030f` gh-148222 "Fix NULL dereference bugs in genericaliasobject.c" | 2026-04-18 | **removed** `Py_DECREF(parameters)` after a failed `_PyTuple_Resize(&parameters, …)` in `_Py_make_parameters` — *the identical shape, same file, 60 lines up* — and did not touch `subs_tvars` |
| `f621ba16b72` gh-150146 "Fix NULL dereference in `_Py_subs_parameters`" | 2026-05-20 | fixed the unchecked `_unpack_args` return; again did not touch `subs_tvars` |

**Guarded twins (three, all in-tree):**
1. `Objects/genericaliasobject.c:549-557` — the *other* `tuple_extend` call site, in
   `_Py_subs_parameters`, which is correct and documents the invariant explicitly:
   ```c
   jarg = tuple_extend(&newargs, jarg, &PyTuple_GET_ITEM(arg, 0), PyTuple_GET_SIZE(arg));
   Py_DECREF(arg);
   if (jarg < 0) {
       Py_DECREF(item);
       Py_XDECREF(tuple_args);
       assert(newargs == NULL);        /* <-- the invariant, asserted */
       return NULL;
   }
   ```
2. `Objects/structseq.c:522-525` — `if (_PyTuple_Resize(&keys, k) == -1) { assert(keys == NULL); return -1; }`
3. `Objects/genericaliasobject.c:243-247` — the post-gh-148222 state of `_Py_make_parameters`.

**How a Python-level input reaches it.**

```python
from typing import TypeVarTuple
Ts = TypeVarTuple('Ts')
alias = dict[str, tuple[*Ts]]
alias[int, str]          # -> ga_getitem -> _Py_subs_parameters -> subs_tvars
```
`subs_tvars` is entered for the `tuple[*Ts]` argument (has `__parameters__`, no
`__typing_subst__`); `param` is the TypeVarTuple (`tp_iter != NULL`), `argitems[0]` is the
tuple `(int, str)`, so `tuple_extend(&subargs, 0, …, 2)` resizes `subargs` from 1 to 2. Any
allocation failure at that resize crashes the interpreter. The same entry point is reachable
through `unionobject.c:349 union_getitem` → `_Py_subs_parameters` (declared in
`pycore_unionobject.h:18-19`), so `typing.Union[…]` subscription reaches the same code.

**Differential — reproduced, with a stack:**

```
$ ASAN_OPTIONS=handle_segv=0 gdb -batch -ex run -ex 'bt 6' --args ./python repro.py 13
Program received signal SIGSEGV, Segmentation fault.
0x... in Py_DECREF (lineno=302, op=0x0, ...) at ./Include/refcount.h:397
#0  Py_DECREF (lineno=302, op=0x0, ...) at ./Include/refcount.h:397
#1  subs_tvars (obj=..., params=..., argitems=..., nargs=...) at Objects/genericaliasobject.c:302
#2  _Py_subs_parameters (...) at Objects/genericaliasobject.c:529
#3  ga_getitem (self=..., item=...) at Objects/genericaliasobject.c:589
#4  PyObject_GetItem (o=..., key=...) at Objects/abstract.c:163
#5  _PyEval_EvalFrameDefault (...)
```

`op=0x0` — an unambiguous NULL dereference. Reproduced independently at two distinct injected
allocation indices (n=13 and n=27 with `_testcapi.set_nomemory(n, n+1)`), the second via a
*nested* `subs_tvars` (frame #5 was `subs_tvars:313`), i.e. the recursion multiplies the
exposure. Reproducer: `repro/repro_one.py` + `repro/oom_sweep.py --direct ga_tvt N`.

**Fix.** Delete line 302. `_PyTuple_Resize` already disposes of the old tuple on every failure
path (`Py_XDECREF(v)` / `PyObject_GC_Del(v)`), so there is nothing to release — that is exactly
what gh-148222 concluded for `_Py_make_parameters` and what `structseq.c:523` asserts.
`Py_DECREF(subparams)` on line 301 is correct and must stay.

**Classification: FIX.** Python-reachable (pure `typing` code, no C API), crashes the
interpreter with SIGSEGV rather than raising `MemoryError`, is a 4-month-old regression in a
file that is #1 on the recency-weighted watchlist, and the correct handling is already written
three times elsewhere in the same tree. OOM class O5.

---

### CONSIDER

#### `Objects/lazyimportobject.c:87-95` · `lazy_import_name` · repr-after-`tp_clear` reads NULL members

```c
static PyObject *
lazy_import_name(PyLazyImportObject *m)
{
    if (m->lz_attr != NULL) {
        if (PyUnicode_Check(m->lz_attr)) {
            return PyUnicode_FromFormat("%U.%U", m->lz_from, m->lz_attr);
        }
        ...
    }
    return Py_NewRef(m->lz_from);          /* lz_from may be NULL after tp_clear */
}
```

`lz_from` is non-NULL at construction (`_PyLazyImport_New:17` rejects a NULL/non-str name) and
becomes NULL only via `lazy_import_clear`. Both `PyUnicode_FromFormat("%U", NULL)` and
`Py_NewRef(NULL)` dereference NULL in a release build. Reaching it requires repr-ing a
lazy-import object *after* the GC has run `tp_clear` on it — exotic, and shared with many
CPython types.

**Guarded twin:** `Objects/cellobject.c:117-128` `cell_repr`, which handles exactly this and is
the closest structural sibling in the sample:
```c
PyObject *ref = PyCell_GetRef((PyCellObject *)self);
if (ref == NULL) {
    return PyUnicode_FromFormat("<cell at %p: empty>", self);
}
```

**Classification: CONSIDER.** `lazyimportobject.c` is the newest type in `Objects/` with **zero**
bug-fix commits ever, so the asymmetry against `cell_repr` is worth a one-line guard, but I
could not construct a Python-level path and will not inflate it to FIX.

**Read end-to-end as instructed; the rest of the file is clean.** Notable clean results:
`_PyLazyImport_New` uses the non-zeroing `PyObject_GC_New` but has **no fallible call between
the allocation and the last member initialisation** (`Py_XNewRef`/`Py_NewRef` cannot fail, and
`m->lz_code = NULL` precedes the only conditional block), so it is *not* an instance of
`dealloc-of-uninitialized-object` — it is in fact the **guarded twin** for the
`template_iter` bug below. `PyLazyImport_Type` is a static type with `tp_base == NULL` and no
`tp_new`, so `type_ready_set_new` (`typeobject.c:9430`) stamps it
`Py_TPFLAGS_DISALLOW_INSTANTIATION`: `lazy_import.__new__(lazy_import)` cannot produce a
zeroed instance, closing the `T.__new__(T)` uninitialised-payload route.

---

### POLICY / ACCEPTABLE

All 21 scanner candidates, grouped by the FP class that dismisses them. Every line number
below is the **corrected** one (see Toolkit assessment — the scanner's own line numbers are
wrong in 21/21 cases).

**FP class "infallible-by-construction returns" — 0 instances.** Not a source of noise here;
the scanner's API list contains only genuinely fallible calls.

**FP class "checked via a macro the scanner doesn't model" (`Py_SETREF`/`Py_XSETREF`/`Py_CLEAR`) — 4 instances:**
`capsule.c:257` (`Py_SETREF(object, object2); if (!object) goto EXIT;`) ·
`genericaliasobject.c:427` and `:430` (`Py_SETREF(item, tmp); if (item == NULL) return NULL;`) ·
`templateobject.c:35`→ reported for the `Py_SETREF(item, PyIter_Next(...))` region.

**FP class "result is returned directly; NULL propagation *is* the error handling" — 9 instances**
(this is the dominant FP class in the sample, ~43%):
`cellobject.c:124` `cell_repr` · `descrobject.c:615` `calculate_qualname` ·
`lazyimportobject.c:106` `lazy_import_repr` · `odictobject.c:1458` `odict_repr` (via `Done:`) ·
`weakrefobject.c:226`, `:231` `weakref_repr` · `weakrefobject.c:617`, `:623` `proxy_repr` ·
`weakrefobject.c:785` `proxy_iternext` (`PyIter_Next` → NULL is the StopIteration protocol).

**FP class "NULL check is the loop condition" — 2 instances:**
`odictobject.c:2234` `while ((pair = PyIter_Next(iterator)) != NULL)` ·
`odictobject.c:2314` `while (res == 0 && (key = PyIter_Next(iterator)))`.

**FP class "check is on the struct-field lvalue, scanner captured the bare field name" — 2 instances:**
`unionobject.c:145` (`ub->args = PyList_New(0)` checked as `if (ub->args == NULL)`) ·
`unionobject.c:174` (`ub->unhashable_args`, same).

**FP class "check is on an aliased lvalue" — 2 instances:**
`genericaliasobject.c:192` and `:460` — `args = tuple_args = PySequence_Tuple(args);` is checked
two lines later as `if (args == NULL)`; the scanner captured `tuple_args` (the innermost
assignment target) and looked for a check on that name.

**Correct by construction — 2 instances:**
`genericaliasobject.c:647` `ga_call` — the unchecked `obj` is passed to `set_orig_class`, whose
first statement is `if (obj != NULL)`; NULL is forwarded to the caller unchanged (correct: the
exception is already set by `PyObject_Call`).
`tupleobject.c:1068` `_PyTuple_Resize` — `*pv = PyTuple_New(newsize); return *pv == NULL ? -1 : 0;`
is the check.

---

## New siblings of known shapes

### Cross-confirm: `dealloc-of-uninitialized-object` at `Objects/templateobject.c:232` / `:239`

Not my bug class, but my OOM sweep reproduced it, so recording it here as a **live** confirmation
of catalog entry **gh-151815 (`template_iter`:225)** — and, importantly, the code is
**byte-identical at HEAD `4f3be1b5777`**, i.e. still unfixed:

```c
templateiterobject *iter = PyObject_GC_New(templateiterobject, &_PyTemplateIter_Type);  /* non-zeroing */
if (iter == NULL) return NULL;
PyObject *stringsiter = PyObject_GetIter(self->strings);
if (stringsiter == NULL) {
    Py_DECREF(iter);            /* :232  members are still garbage */
    return NULL;
}
PyObject *interpolationsiter = PyObject_GetIter(self->interpolations);
if (interpolationsiter == NULL) {
    Py_DECREF(iter);            /* :239  same */
    Py_DECREF(stringsiter);
    return NULL;
}
iter->stringsiter = stringsiter;          /* members initialised only here */
```

```
#0  Py_DECREF (lineno=53, op=0xcdcdcdcdcdcdcdcd, ...) at ./Include/refcount.h:397
#1  templateiter_clear (op=...) at Objects/templateobject.c:53
#2  templateiter_dealloc (op=...) at Objects/templateobject.c:45
#3  _Py_Dealloc (op=...) at Objects/object.c:3312
#4  Py_DECREF (lineno=232, op=...) at ./Include/refcount.h:410
#5  template_iter (op=...) at Objects/templateobject.c:232
#6  PyObject_GetIter (o=...) at Objects/abstract.c:2825
```

`0xcdcdcdcdcdcdcd` is the debug allocator's uninitialised fill — conclusive. Triggered from
pure Python by `list(t"a{x}b{x}c")` and `list(t"a{x}" + t"b{x}")` under OOM injection.
**Guarded twin:** `_PyLazyImport_New` (`lazyimportobject.c:29-48`), and the sibling
`_PyTemplate_Build` (`templateobject.c:405-416`) which has no fallible step after the
allocation at all. Handing to `uninit-dealloc-checker` for classification.

### Sibling hunt for the crown shape (`Py_DECREF` of an out-param the callee NULLed)

I swept the whole tree — `Objects/`, `Modules/`, `Python/`, `Parser/` — for
`_PyTuple_Resize` / `_PyBytes_Resize` / `PyUnicode_Resize` **plus auto-discovered local
wrappers** (`tuple_extend`, `PyBytes_Concat`, `arrange_output_buffer_with_maximum`).
Prototype: `repro/proto_rule_r1.py`.

| site | verdict |
|---|---|
| `Objects/genericaliasobject.c:297` → `:302` `Py_DECREF(subargs)` | **the FIX above** |
| `Objects/structseq.c:522` → `:531`/`:535` | clean — those `Py_DECREF(keys)` are on the success/other paths, and `:523` asserts `keys == NULL` |
| `Modules/_pickle.c:1417` → `:1423`/`:1430` | clean — the `_PyBytes_Resize` failure branch is `{ return -1; }` with no DECREF |
| `Objects/genericaliasobject.c:243`, `:258`, `Objects/codeobject.c:869`, `Modules/_tkinter.c:3359`, `:3431`, `Modules/itertoolsmodule.c:248`, `Objects/genericaliasobject.c:174`, `:549` | clean — all use `Py_XDECREF`, `assert(x == NULL)`, or no DECREF at all |

**Conclusion: the `subs_tvars` site is the only unfixed instance of this shape in CPython.**
It is a genuine clean sweep, not an unfinished one.

---

## Classes bounded (clean negatives)

1. **OOM-driven NULL dereference across the sample's Python surface — bounded to 2 sites.**
   I built a fork-per-injection harness (`repro/oom_sweep.py`): 22 workloads exercising
   all 14 sample files (generic-alias subscription incl. TypeVarTuple, `__parameters__`, repr,
   call; union construction/repr/subscription; t-string build/iterate/concat; weakref +
   weakproxy repr; OrderedDict repr/update/copy; `os.stat` structseq; descriptor `__qualname__`;
   closure cell repr; `iter()`/`iter(callable, sentinel)`; tuple/function construction),
   × `_testcapi.set_nomemory(n, n+1)` for **n ∈ [0, 200)** = 4,400 injected failures, each in a
   forked child with a 2 s watchdog, ASan `handle_segv=0` so a child dies with a clean SIGSEGV.
   **Result: exactly 4 crashes at 2 distinct sites** — `subs_tvars:302` (n=21, n=37) and
   `template_iter:232/239` (n=5, n=10). Everything else raised `MemoryError` cleanly or
   completed. Range [60, 200) produced **zero** crashes.
2. **`PyArg_Parse*` return values — clean.** The agent brief calls out unchecked
   `PyArg_ParseTuple` as a bug class. In the sample there are **no** `PyArg_ParseTuple` calls
   at all; the 13 argument-parsing calls (`PyArg_UnpackTuple`, `_PyArg_NoKeywords`,
   `_PyArg_NoKwnames`, `_PyArg_CheckPositional`, `_PyArg_UnpackStack`, `_PyArg_NoPositional`
   in `tupleobject.c`, `genericaliasobject.c`, `descrobject.c`, `weakrefobject.c`,
   `funcobject.c`, `structseq.c`, `cellobject.c`) are **all** guarded by `if (!…)`. The one
   uncovered form, `weakrefobject.c:466`, is `return PyArg_UnpackTuple(...)` — propagated.
   This class is clean in this scope. (It is also not implemented in the scanner — see below.)
3. **Integer overflow in an allocation multiply — none in the sample.** No
   `PyMem_*/malloc(n * size)` where `n` derives from a Python-controlled `Py_ssize_t` exists in
   the 14 files; the only raw `PyMem_Malloc` is `capsule.c:236`
   (`(strlen(name) + 1) * sizeof(char)` — `sizeof(char) == 1`, C-caller-supplied, not Python
   input). `_PyTuple_Resize`/`PyTuple_New` sizes go through the allocator's own overflow checks.
   Correctly not my class — the briefing assigns it to `memory-pattern-analyzer`.
4. **Deref-before-check (`Py_TYPE`/`Py_SIZE`/`PyList_GET_ITEM`/`->` on a possibly-NULL local)
   — no instances found by reading in the sample.** But note this is *my* reading, not the
   scanner's: the scanner's rule for this is dead code (below), so its `high_confidence: 0`
   across all 113 `Objects/` findings carries **no** information.

---

## Toolkit assessment

### Precision

**0 of 21 sample candidates (0%), 21 dismissals across 6 FP classes.** Two of the six are in
the published taxonomy; **four are new** and should be added to `data/cpython_non_bugs.md`.

| FP class | n | in taxonomy? | root cause in `scan_null_checks.py` |
|---|---|---|---|
| result returned directly | 9 | **new** | rule 1 has no notion of "the variable is only ever `return`ed / `goto`-ed to a returning label" |
| `Py_SETREF`/`Py_XSETREF`/`Py_CLEAR` | 4 | yes | `_NULL_CHECK_TEMPLATE` models only 3 literal `if` forms |
| NULL check is the loop condition | 2 | **new** | the check is on the *same* line as the assignment, and `after = clean[m.end():]` starts after it |
| check on the struct-field lvalue | 2 | **new** | `_ALL_ALLOC_RE`'s `(\w+)\s*=` captures only the last identifier of `ub->args` |
| check on an aliased lvalue | 2 | **new** | `a = b = API()` — the innermost target is captured, the outer one is checked |
| correct by construction (callee is NULL-tolerant / the check is the `return` expression) | 2 | — | interprocedural; out of reach for a single-function regex pass |

There is a self-reinforcing problem here: **`PyUnicode_FromFormat` alone accounts for 8 of the
21 sample candidates and all 8 are the "returned directly" class**, because `*_repr` slots are
overwhelmingly `res = PyUnicode_FromFormat(...); return res;`. Any repr-heavy file will produce
pure noise until that class is gated.

### Recall gaps

**RG-1 (critical) — the crown bug's entire shape is unmodelled.** `Py_DECREF(x)` where `x` was
NULLed by an out-param API that failed. The scanner only knows about *unchecked allocations*;
here the value is not an allocation result at all — it is a variable the callee zeroed. This
shape produced a reproducible SIGSEGV in the highest-priority file in the sample and the
scanner is structurally incapable of seeing it. Prototype implementation and tree-wide yield:
5 candidates, **1 true positive, 4 FPs all of the form "the DECREF is not inside the failure
branch"** — a brace-depth gate takes it to 5/5 precision. See `repro/proto_rule_r1.py`.

**RG-2 (critical) — the `deref-before-check` rule is dead code.** In
`analyze_function_null_safety`, section *"2. Dereference before NULL check"* builds
`ptr_vars_checked`, iterates `_DEREF_RE`, `continue`s past a few names — and **never appends a
finding**. The loop body's only remaining content is the comment
`# This is a simplified heuristic.` `_PTR_DEREF_RE` is defined at module level and never
referenced. Consequences: (a) `high_confidence` can only ever come from rule 1's 200-character
lookahead, which is why it is **0 across all 113 `Objects/` findings**; (b) the agent
definition's Phase 1 instruction — *"Prioritize high-confidence findings (allocation result
dereferenced without check) — these are potential crash bugs"* — directs the agent at a
permanently empty set; (c) the module docstring's claim "Detects dereferences before NULL
checks" is false.

**RG-3 (critical) — every reported line number is wrong.** Measured mechanically
(`repro/check_lines.py`): **0/21 findings in the sample and 2/113 across all of
`Objects/` land on a line that actually contains `<var> = <api>(`.** Two independent causes:
- A constant **+2** off-by-two in `analyze()`'s
  `finding["line"] = func["start_line"] + finding.pop("line_offset")`. `find_functions` sets
  `start_line = sig_start + 1` where `sig_start` may additionally be decremented when the
  return type is on its own line — the near-universal CPython style
  (`static PyObject *\nfoo(...)\n{`).
- `strip_comments_and_strings` replaces `/*…*/` with a **single space**, destroying newlines,
  so every finding after a block comment inside the same function drifts further. Observed
  offsets in the sample: `{-1, +2, +3, +8}`; across `Objects/`: `{-3 … +16}`.

  `genericaliasobject.c` reported `452`; the real site is `460` — the eight lines are the
  `/* Replace all type variables … */` comment block. A finding reported at the wrong line is
  not citable in an upstream bug report.

**RG-4 (high) — the fallible-API list is a closed enum missing ~200 sites in `Objects/` alone.**
`PYOBJ_APIS` has 27 entries. Measured over `Objects/*.c`, assignment sites from fallible
NULL-returning APIs that are **absent** from the list: **202**. Top by count:
`PyObject_GetIter` 34 · `PyObject_CallOneArg` 24 · `_PyObject_CallNoArgs` 19 ·
`_PyObject_LookupSpecial` 15 · `PyLong_FromSsize_t` 12 · `PyType_GenericAlloc` 11 ·
`_PyEval_GetBuiltin` 9 · `PyObject_Vectorcall` 8 · `PyUnicodeWriter_Create` 8 ·
`PyObject_CallNoArgs` 7 · `PySet_New` 6 · `PySequence_Fast` 6. Widening the list without first
fixing precision would be actively harmful (it would multiply the "returned directly" class),
which is why the ordering in the proposals below matters.

**RG-5 (medium) — `PyArg_Parse*` is claimed but not implemented.** The module docstring says
"and PyArg_Parse* issues"; the agent definition says "PyArg_ParseTuple without checking return
value: if the parse fails and the return isn't checked, extracted arguments are garbage."
There is no code for this anywhere in the 317 lines. The class happens to be clean in this
sample, but a maintainer reading "0 findings" would draw the wrong conclusion.

**RG-6 (medium) — 500/300/200-character lookahead windows are byte-based.** Rule 1 searches for
the NULL check in `after[:300]` and a deref in `after[:200]`. In CPython's `Objects/` a single
error branch (`Py_DECREF` ×3 + `PyErr_Format` + `return`) routinely exceeds 300 bytes, so a
check that *is* present three statements later is missed. A statement- or line-count window
(e.g. 10 lines) is both cheaper to reason about and more faithful.

### Prompt issues

1. The agent definition says *"Prioritize high-confidence findings (allocation result
   dereferenced without check)."* Because of RG-2 that set is always empty; the prompt should
   not lead with a triage axis the scanner cannot populate.
2. The definition asks for "Integer overflow in allocation size" and "unsafe string operations
   (`sprintf` → `snprintf`, `strcpy` → `strncpy`)". Neither is in the scanner, and per
   `informed_briefing.md` the overflow shape belongs to `memory-pattern-analyzer`. In a
   multi-agent run this creates duplicate/contradictory ownership. The
   `sprintf`/`strcpy` guidance is C-extension boilerplate that does not fit CPython core
   (`Objects/` has essentially none) and dilutes the prompt.
3. The definition's classification guide has no entry for the shape that actually mattered
   ("DECREF of a pointer a callee NULLed"). Adding the guarded-twin framing for it — *"if the
   callee takes `PyObject **`, check whether it NULLs on failure before you accept a DECREF in
   the failure branch"* — would have pointed me there directly.
4. Nothing in the definition suggests **OOM injection** as the differential for this bug class,
   even though `set_nomemory` is the only way to reach almost every finding this scanner
   produces (allocation-failure paths are unreachable otherwise). The v0.7 OOM reproducer
   harness exists in the toolkit but the null-safety agent does not reference it. Both of this
   run's real crashes came from it.

### Concrete tuning proposals (ranked)

**P1 — Fix the line numbers.** Two one-line changes, unblocks everything else.
```python
# scan_null_checks.py, strip_comments_and_strings():
source = re.sub(r'/\*.*?\*/', lambda m: '\n' * m.group(0).count('\n'),
                source, flags=re.DOTALL)
```
and in `find_functions`, record the body's first line explicitly
(`"body_line": body_start + 1`) and use `finding["line"] = func["body_line"] + line_offset - 1`
in `analyze()`. Add a regression test asserting that for every finding,
`source_lines[line-1]` matches `rf'{var}\s*=\s*(?:\([^)]*\)\s*)?{api}\s*\('`. This is
mechanically checkable — `repro/check_lines.py` is a ready-made harness.

**P2 — New finding type `decref_of_nulled_outparam`, confidence `high`.** The only rule in this
report that found a real bug. Data-driven: add
`data/nulling_outparam_apis.json = ["_PyTuple_Resize", "_PyBytes_Resize", "PyUnicode_Resize", "_PyUnicode_Resize"]`,
auto-discover local wrappers (a function with a `PyObject **` parameter that forwards it to a
listed API), then flag `Py_DECREF(<var>)` occurring **inside the failure branch** of
`<api>(&<var>, …)`. Gate on brace depth so the DECREF must be inside the `if (… < 0) { … }`
block — that turns the prototype's 5 tree-wide candidates into 1 finding, 0 FPs. Emit the
`assert(x == NULL)` sibling (`structseq.c:523`, `genericaliasobject.c:555`) as the guarded twin
in the finding text. Working prototype: `repro/proto_rule_r1.py`.

**P3 — Gate rule 1 on "the value is actually used dangerously."** Suppress a candidate when,
within the lookahead window, the variable is *only* (a) `return`ed, (b) assigned into
`Py_SETREF`/`Py_XSETREF`/`Py_CLEAR`, (c) passed to a `Py_XDECREF`, or (d) the subject of a
`goto <label>` whose label body only `return`s it. On this sample that removes 13 of 21
candidates outright. Concretely, in `analyze_function_null_safety` extend `_NULL_CHECK_TEMPLATE`
with
```
r'Py_X?SETREF\s*\(\s*\w+\s*,\s*{var}\s*\)|'
r'Py_CLEAR\s*\(\s*{var}\s*\)|'
r'return\s+{var}\s*;|'
r'while\s*\(.*\b{var}\b.*\)'      # NULL check as the loop condition
```
and additionally search the **whole match line**, not just `after`, so
`while ((pair = PyIter_Next(it)) != NULL)` self-discharges.

**P4 — Capture the full lvalue, not the trailing identifier.** Change `_ALL_ALLOC_RE`'s target
group from `(\w+)` to `((?:\w+\s*(?:->|\.))*\w+)` and `re.escape` it into
`_NULL_CHECK_TEMPLATE`; separately, handle chained assignment `a = b = API()` by registering
*every* target on the line as satisfying the check. Removes 4 of 21.

**P5 — Implement or delete the dead `deref-before-check` block (RG-2).** Minimum viable version:
within a function, for each variable assigned from a listed API, flag the first subsequent
occurrence of `var->`, `*var`, `Py_TYPE(var)`, `Py_SIZE(var)`, `PyTuple_GET_ITEM(var`,
`PyList_GET_ITEM(var`, `PyUnicode_GET_LENGTH(var` that precedes any NULL check — emit
`type: "deref_before_check"`, `confidence: "high"`. If it is not implemented in this slice,
delete the block, drop the claim from the module docstring, and remove
"Prioritize high-confidence findings" from the agent definition, because today all three lie
about the same non-existent capability.

**P6 — Widen `PYOBJ_APIS`, but only after P3/P4 land.** Suggested additions, ordered by measured
`Objects/` frequency: `PyObject_GetIter`, `PyObject_CallOneArg`, `_PyObject_CallNoArgs`,
`PyObject_CallNoArgs`, `_PyObject_LookupSpecial`, `PyObject_Vectorcall`,
`PyObject_CallFunctionObjArgs`, `PyType_GenericAlloc`, `PyUnicodeWriter_Create`, `PySet_New`,
`PyDict_Copy`, `PyDict_Items`, `PyDict_Keys`, `PySequence_Fast`, `PyUnicode_Concat`,
`PyUnicode_Substring`, `PyUnicode_FromStringAndSize`, `PyLong_FromSsize_t`,
`PyStructSequence_New`, `_PyEval_GetBuiltin`. Move the list to `data/fallible_apis.json` so it
is refreshable per release instead of frozen in source.

**P7 — Add four FP classes to `data/cpython_non_bugs.md`** under "NULL checks / error paths":
*result returned directly (NULL propagation is the contract — dominant in `*_repr` slots)*;
*NULL check is the loop condition*; *check is on the struct-field lvalue*; *check is on an
aliased lvalue (`a = b = API()`)*. Each with the exemplar `file:line` from this run.

**P8 — Reference the OOM harness from the agent definition.** Add a Phase 4 to
`null-safety-scanner.md`: *"For every surviving candidate on an allocation-failure path, drive
it with `_testcapi.set_nomemory(n, n+1)` under a fork-per-injection sweep; a SIGSEGV instead of
a clean `MemoryError` promotes the finding to FIX with a differential."* Ship
`repro/oom_sweep.py` as a toolkit script (`scripts/oom_sweep.py`) — a generic
workload-list × injection-index driver with a per-child watchdog. Both real crashes in this run
came from it, and neither was in the scanner output.
