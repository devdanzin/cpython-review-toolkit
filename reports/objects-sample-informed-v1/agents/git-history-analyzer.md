# git-history-analyzer — Objects/ sample (informed, Group E)

**Window:** full history, `--days 13000 --max-commits 20000 --no-function` over `Objects/`
(1990-12-20 → 2026-07-24, **9,203 commits**, 168 files, 442 authors, `commit_cap_applied: false`).
Runtime ~11 s after this session's `UnicodeDecodeError` fix. Raw JSON:
`/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/hist_objects_full.json` (13.4 MB).

## Scanner volume

This agent consumes other agents' findings rather than producing its own candidate list.

| | count |
|---|---|
| confirmed findings cross-referenced | 9 |
| introducing commits identified | **9 of 9** |
| fixing commits judged **incomplete** | **7** |
| tree-wide similar-bug sweeps run | 5 (2 shapes × strict/relaxed + uninit-dealloc) |
| **new** unfixed instances found outside `Objects/` | **2** (1 FIX, 1 CONSIDER) |
| clean tree-wide negatives established | 3 |

---

## 1. Fix-completeness verdicts

Every confirmed finding, its introducing commit, and whether the commit that fixed a
*sibling* site should have caught it.

### V-1 · `Objects/iterobject.c:80` `iter_iternext` — **INCOMPLETE, three times over** ★★★

**Introduced:** `fbb1c5ee068d` (2016-03-30, Issue #26494, Serhiy Storchaka) —
*"Fixed crash on iterating exhausting iterators."* The diff **reordered** the two statements:

```c
-        Py_DECREF(seq);
-        it->it_seq = NULL;
+        it->it_seq = NULL;
+        Py_DECREF(seq);
```

**What was missed.** The reorder fixes only the *inner* half of the re-entrancy: it stops
`it->it_seq` from dangling *while* the `Py_DECREF` runs a `__del__`. It does nothing about
the *outer* half — `seq` is a stale snapshot taken at `:61` (`seq = it->it_seq;`) and carried
across `PySequence_GetItem(seq, …)` at `:70`, which runs a user `__getitem__`. If that
`__getitem__` re-enters `next(it)`, the inner frame executes `:79-80` first, drops the last
reference, and the outer frame then DECREFs the freed object again. The correct fix is
`Py_CLEAR(it->it_seq)` — read the field afresh, NULL-then-DECREF — which makes the second
pass a no-op.

**Three later commits fixed this exact shape elsewhere and skipped `iter_iternext`:**

| commit | date | what it fixed | scope | `iter_iternext`? |
|---|---|---|---|---|
| `54dfa14c5a9` gh-101765 | 2023-02-24 | "iterator pointers accessed after a call that can run arbitrary Python" — hoisted `_PyEval_GetBuiltin` above the field reads | **8 files**, `__reduce__` only | untouched |
| `705487c6557` gh-101892 | 2023-03-04 | `calliter_iternext`, **same file, 160 lines below** — added the `it->it_sentinel != NULL` recheck after `_PyObject_CallNoArgs` | 1 function | untouched |
| `1fb7e2aeb7e4` gh-120608 | 2025-03-12 | `reversed_next` (`Objects/enumobject.c:440`) — the *other* generic `PySequence_GetItem` iterator — for free-threading | 1 function | untouched |

`54dfa14c5a9` even left the comment `/* _PyEval_GetBuiltin can invoke arbitrary code, call must
be before access of iterator pointers. see issue #101765 */` at `iterobject.c:112` — **31 lines
below the bug**, describing the bug.

**Guarded twins, both in the guarded state at HEAD:**
- `Objects/iterobject.c:243,249` `calliter_iternext` — `Py_CLEAR(it->it_callable)` / `Py_CLEAR(it->it_sentinel)`.
- `Objects/enumobject.c:456-459` `reversed_next` — `Py_CLEAR(ro->seq)` **plus** an
  `#ifndef Py_GIL_DISABLED` opt-out that deliberately leaks under FT rather than race.

`Objects/iterobject.c` has received **zero** free-threading commits (verified: `git log --since=2024-01-01`
returns only signature/UBSan/docs churn). The FT sweep reached `enumobject.c` in 2025-03 and
`genericaliasobject.c` in 2026-07 and never arrived.

---

### V-2 · `Objects/genericaliasobject.c:542` `_Py_subs_parameters` heap-UAF — **INTRODUCED by a fix; two later fixes to the same function missed it** ★★★

**Introduced:** `1da989be74e` (2025-09-11, gh-138479, Peter Bierma) — added the
`!PyTuple_Check(arg)` error branch:

```c
Py_DECREF(newargs);
Py_DECREF(item);
Py_XDECREF(tuple_args);                              /* :541  frees the tuple  */
PyObject *original = PyTuple_GET_ITEM(args, iarg);   /* :542  args ALIASES it  */
PyErr_Format(…, "…%T…%T", original, arg);            /* :543  %T derefs it     */
```

The aliasing is set up 68 lines earlier: `args = tuple_args = PySequence_Tuple(args);` when
the caller passes a list. The author cloned the cleanup block from the `arg == NULL` branch
eight lines above (`:534-537`), which is correct *because it reads nothing afterwards*, then
appended a read.

**What was missed by whom.**
- `8060aa5d7dd` (2026-03-09, gh-145376, *"Fix various refleaks in Objects/"*) edited
  `_Py_subs_parameters` — it added `Py_DECREF(item)` to the `PySequence_Tuple` failure path
  **12 lines above the aliasing setup**. A refleak sweep that touched the very line establishing
  the `args`/`tuple_args` alias did not notice the alias was consumed after free.
- `f621ba16b72` (2026-05-20, gh-150146) added a NULL check at the top of the same function.

**Verdict: INCOMPLETE.** Both sweeps were single-error-class sweeps on a function carrying
four undocumented invariants.

---

### V-3 · `Objects/genericaliasobject.c:302` `subs_tvars` `Py_DECREF(NULL)` — **INTRODUCED by one fix, then explicitly left behind by the next** ★★★

**Introduced:** `8060aa5d7dd` (2026-03-09, gh-145376, *"Fix various refleaks in Objects/"*):

```c
 j = tuple_extend(&subargs, j, &PyTuple_GET_ITEM(arg, 0), PyTuple_GET_SIZE(arg));
 if (j < 0) {
+    Py_DECREF(subparams);
+    Py_DECREF(subargs);       /* subargs is NULL here */
     return NULL;
 }
```

`tuple_extend` (`:170`) forwards `_PyTuple_Resize(dst, …)`, which sets `*dst = NULL` and
DECREFs on failure. The "refleak fix" added a DECREF of a pointer the callee had already
freed and nulled → NULL deref, and a double-free of `subparams` if it were reachable.

**Left behind by:** `634568d030f` (2026-04-18, gh-148222, **titled "Fix NULL dereference bugs
in genericaliasobject.c"**), five weeks later. That commit removed the *structurally identical*
line in `_Py_make_parameters`:

```c
 if (_PyTuple_Resize(&parameters, len) < 0) {
     Py_DECREF(subparams);
-    Py_DECREF(parameters);
     Py_XDECREF(tuple_args);
     return NULL;
 }
```

Same file, same idiom, **60 lines apart**, plural in the title, and the second instance was
five weeks old at the time. **Verdict: INCOMPLETE — the clearest single miss in the sample.**

The correct pattern already exists twice in-tree and both times it is spelled with an
`assert`: `genericaliasobject.c:554` (`assert(newargs == NULL);`, added by `1da989be74e`) and
`Objects/structseq.c:523` (`assert(keys == NULL);`). Neither assert was propagated to `subs_tvars`.

---

### V-4 · `Objects/genericaliasobject.c:583` `ga_getitem` — **INCOMPLETE FT fix, 7 days before HEAD** ★★★

**Fix:** `68abf17fa92` (2026-07-08, gh-153298, sobolevn) split `ga_parameters` into
`ga_parameters_lock_held()` + a `Py_BEGIN_CRITICAL_SECTION(self)` wrapper.

**What was missed.** `alias->parameters` has exactly **two** lazy-init sites and the commit
guarded one:

| site | function | at HEAD |
|---|---|---|
| `:848` | `ga_parameters_lock_held` | `Py_BEGIN_CRITICAL_SECTION` ✅ |
| `:583-588` | `ga_getitem` | **unguarded** ❌ |

Both write the same field with the same three lines. `ga_getitem` is `tp_getitem` — `alias[int]`
from two threads races the same field the commit just locked. The commit even shipped
`Lib/test/test_free_threading/test_types.py`, exercising `__parameters__` but not `alias[…]`.

`ga_getitem` additionally passes `alias->parameters` **borrowed** into `_Py_subs_parameters`,
which runs user `__typing_subst__` — the borrowed-ref-across-call shape, on the field that
was just declared racy. **Verdict: INCOMPLETE.**

---

### V-5 · `Objects/unionobject.c:172` unconditional `PyErr_Clear()` — **not a fix at all; a feature that skipped the established idiom** ★★

**Introduced:** `dc6d66f44c0` (2025-03-04, gh-105499, *"Merge typing.Union and types.UnionType"*)
— a 438-line rewrite of `unionobject.c`. `unionbuilder_add_single_unchecked` clears **any**
exception from `PyObject_Hash(arg)`, so a user `__hash__` raising `KeyboardInterrupt`,
`MemoryError`, or a domain `ValueError` is silently reclassified as "unhashable".

**Guarded twin (pre-dating it by years):** `Objects/setobject.c:2559` `_PySet_Contains` and
`:2619` — the same "hash failed → fall back" decision, filtered:

```c
Py_hash_t hash = PyObject_Hash(key);
if (hash == -1) {
    if (!PySet_Check(key) || !PyErr_ExceptionMatches(PyExc_TypeError)) {
        set_unhashable_type(key);
        return -1;
    }
    PyErr_Clear();
```

**Verdict: no fixing commit exists.** This is the one finding in the set with no fix history —
it has been live for 17 months and no commit has come near it. `unionobject.c` carries
21 fix-classified commits and **zero** touching this function since introduction.

---

### V-6 · `Objects/structseq.c:77` `PyStructSequence_New` overflow — **INCOMPLETE; the April 2026 fix hardened the reporting of the very value that overflows** ★★★

**Fix:** `69f51625e63` (2026-04-06, gh-148119, *"Refactor `get_type_attr_as_size` to improve
error handling"*) rewrote `get_type_attr_as_size` at `:28`:

```c
-    if (v == NULL && !PyErr_Occurred()) {
+    if (v == NULL) {
+        if (PyErr_Occurred()) {
+            return -1;
+        }
         PyErr_Format(PyExc_TypeError, "Missed attribute '%U' of type %s", …);
         return -1;
     }
     return PyLong_AsSsize_t(v);
```

**What was missed.** The refactor fixed the *NULL* half (previously `v == NULL` with an error
set fell through to `PyLong_AsSsize_t(NULL)`) and left the *range* half untouched.
`PyLong_AsSsize_t(v)` happily returns `2**62` when a Python-writable `n_fields` says so; the
only downstream check is `if (size < 0)` at `:69`. `size` then feeds:

- `:77` `PyObject_GC_NewVar(PyStructSequence, type, size)` — `tp_basicsize + size*tp_itemsize` wraps;
- `:86` `for (i = 0; i < size; i++) obj->ob_item[i] = NULL;` — writes `size` pointers into the
  wrapped allocation → **heap-buffer-overflow WRITE**.

The commit that most recently touched the function producing the unvalidated value did not
bound it. **Verdict: INCOMPLETE — root cause vs. symptom.** The symptom (bad error reporting)
was fixed; the root cause (an unvalidated Python-controlled `Py_ssize_t` reaching an allocation
multiply) was not. Adds evidence to the briefing's `integer-overflow-in-allocation` shape.

---

### V-7 · `Objects/odictobject.c:1952` `odictiter_new` uninit-dealloc — **INCOMPLETE; a 10-file mechanical refactor edited the exact block in both twin and bug and normalised only the twin's spelling** ★★

**Not introduced by a fix** — pre-existing since the `PyTuple_Pack(2, Py_None, Py_None)` era
(verified against `a933e9ccee6^`).

**The near-miss:** `a933e9ccee6` (2026-03-28, GH-145247, *"Use `_PyTuple_FromPair[Steal]` in
Objects"*) touched **10 files** including both `Objects/dictobject.c` and `Objects/odictobject.c`,
rewriting the identical five-line `di->di_result = …; if (NULL) { Py_DECREF(di); }` block in
each. In `dictiter_new` that block is safe because `di_dict`/`di_used`/`len`/`di_pos` are
assigned **before** it (`dictobject.c:5625-5641`). In `odictiter_new` the block comes **first**
and `kind`, `di_current`, `di_size`, `di_state`, `di_odict` are assigned after
(`odictobject.c:1955-1960`), so `Py_DECREF(di)` at `:1952` runs `odictiter_dealloc` over five
garbage members.

**Guarded twin:** `Objects/dictobject.c:5644` `dictiter_new` — fallible call last.
**Verdict: INCOMPLETE** — a cross-file mechanical sweep is the natural place to notice that
two copies of the same block have opposite orderings, and it did not.

---

### V-8 · `Objects/listobject.c:262` `PyList_New` (FT branch) — **INCOMPLETE at birth: a new `#ifdef` arm that did not mirror the existing arm** ★★★

**Introduced:** `17d31bf3843` (2024-03-10, gh-112087, *"Store memory allocation information
into `_PyListArray`"*, Donghee Na) — added the free-threaded arm:

```c
#ifdef Py_GIL_DISABLED
    _PyListArray *array = list_allocate_array(size);
    if (array == NULL) {
        Py_DECREF(op);            /* :262 — op->ob_item never assigned */
        return PyErr_NoMemory();
    }
    memset(&array->ob_item, 0, size * sizeof(PyObject *));
    op->ob_item = array->ob_item; /* :266 — too late */
#else
    op->ob_item = (PyObject **) PyMem_Calloc(size, sizeof(PyObject *));
#endif
    if (op->ob_item == NULL) {
        Py_DECREF(op);            /* :271 — safe: ob_item is NULL */
```

**The guarded twin is the `#else` arm three lines below**, in the same `#ifdef` block. The
non-FT path assigns `op->ob_item` *first*, so its `Py_DECREF(op)` at `:271` finds a NULL
member. The FT path DECREFs before any assignment; `op` came from `_Py_FREELIST_POP` or
`PyObject_GC_New`, both non-zeroing, so `list_dealloc` frees a garbage `ob_item`.

This is the textbook *"the fix covers the Unix path and leaves the `#ifdef` variant"* case
from the agent brief, inverted: the **new** platform arm failed to copy the **old** arm's
ordering. **Verdict: INCOMPLETE — and the reviewer had the correct code on screen.**

---

### V-9 · `Objects/tupleobject.c` `tuple_hash` ↔ `Objects/dictobject.c:8427` `frozendict_pair_hash` — **INCOMPLETE: the fix formalised the copy-paste and propagated the value, not the guard** ★★★

**Commit:** `244300162d2` (2026-05-20, gh-149807, *"Fix `hash(frozendict)`: compute (key, value)
pair hash"*). It fixed `frozendict_pair_hash`'s hash *value* and **added a comment above
`tuple_hash`**:

```c
   If you update this code, update also frozendict_pair_hash() which copied
   this code.
```

matching `dictobject.c:8413`'s `// Code copied from tuple_hash().`

**What was missed.** Neither copy brackets its descent with `Py_EnterRecursiveCall`.
`tuple_hash` loops `PyObject_Hash(item)`; `frozendict_pair_hash` calls `PyObject_Hash(value)`;
`PyObject_Hash` does **not** push a recursion frame. A nested `((((…),),),)` recurses
`tuple_hash → PyObject_Hash → tuple_hash` to native stack exhaustion → SIGSEGV, not
`RecursionError`. **Verdict: INCOMPLETE.** The maintainers documented the duplication link
two months before HEAD and used it to propagate the hash algorithm while leaving the missing
guard duplicated on both sides. `frozendict` is new in this cycle — the copy was made *after*
CPY-0001 / gh-154318 named `tuple_hash` + `frozendict_hash` as a pair.

---

### Verdict summary

| # | finding | fixing commit | verdict |
|---|---|---|---|
| V-1 | `iterobject.c:80` | `fbb1c5ee068d` (+3 later sweeps) | **INCOMPLETE** ×3 |
| V-2 | `genericaliasobject.c:542` | `1da989be74e` **introduced**; `8060aa5d7dd` missed | **INCOMPLETE** |
| V-3 | `genericaliasobject.c:302` | `8060aa5d7dd` **introduced**; `634568d030f` missed | **INCOMPLETE** |
| V-4 | `genericaliasobject.c:583` | `68abf17fa92` | **INCOMPLETE** |
| V-5 | `unionobject.c:172` | *none exists* | **NEVER FIXED** (17 months) |
| V-6 | `structseq.c:77` | `69f51625e63` | **INCOMPLETE** (symptom, not root cause) |
| V-7 | `odictobject.c:1952` | `a933e9ccee6` (near-miss) | **INCOMPLETE** |
| V-8 | `listobject.c:262` | `17d31bf3843` **introduced** | **INCOMPLETE at birth** |
| V-9 | `tupleobject.c` / `dictobject.c:8427` | `244300162d2` | **INCOMPLETE** |

**7 of 9 have a fixing commit, and 7 of 7 are incomplete. 3 of 9 were *introduced* by a commit
whose stated purpose was fixing a bug or improving safety.**

---

## 2. The `genericaliasobject.c` pattern

Six commits touched this 1,070-line file in the 18 months to HEAD. Every one is a point fix
on a single reported reproducer, and **every one is by a different author**:

| commit | date | author | issue | outcome |
|---|---|---|---|---|
| `1da989be74e` | 2025-09-11 | Peter Bierma | gh-138479 | **introduced** the `:542` heap UAF |
| `bdba5f0db2a` | 2026-01-15 | sobolevn | gh-143635 | crash in `ga_repr_items_list` |
| `8060aa5d7dd` | 2026-03-09 | Pieter Eendebak | gh-145376 | **introduced** the `:302` NULL deref |
| `634568d030f` | 2026-04-18 | Prakash Sellathurai | gh-148222 | fixed the twin of `:302`, **left `:302`** |
| `f621ba16b72` | 2026-05-20 | sobolevn | gh-150146 | NULL deref in `_Py_subs_parameters` |
| `68abf17fa92` | 2026-07-08 | sobolevn | gh-153298 | fixed `ga_parameters`, **left `ga_getitem`** |

The file's raw churn is unremarkable (64 commits lifetime, 6 in 2026 — rank 30-ish in `Objects/`).
Recency-weighted by *crash-class* fixes it is **#1 in the sample**: 10 of its 29 fix-classified
commits landed after 2024.

### What is actually going wrong

Not velocity — **invariant density with no owner and no documentation.** Three mutually
recursive functions (`_Py_make_parameters` `:186`, `subs_tvars` `:279`, `_Py_subs_parameters`
`:406`) share four non-obvious invariants, **none of which is stated anywhere in the file**:

1. **`args` may alias `tuple_args`.** `args = tuple_args = PySequence_Tuple(args)` — so
   `Py_XDECREF(tuple_args)` invalidates `args`. → V-2.
2. **`_PyTuple_Resize` / `tuple_extend` NULL their out-param on failure.** A `Py_DECREF` after
   a failed resize is a NULL deref, and the correct spelling is nothing at all. → V-3.
3. **`alias->parameters` is a lazy cache with two write sites**, one of which is a `tp_getitem`
   slot. → V-4.
4. **`item` becomes owned at `_unpack_args()`** and must be released on each of ~9 error paths.
   → `f621ba16b72`, `8060aa5d7dd`.

Each arriving contributor rediscovers exactly one invariant, patches their instance, and leaves
the others. `8060aa5d7dd` (invariant 4) violated invariant 2 in the same diff.
`634568d030f` learned invariant 2 and applied it to one of two sites. `1da989be74e` cloned a
cleanup block that was correct only because it read nothing afterwards.

**The file is not churning because it is unstable; it is unstable because each fix is authored
without the context the previous fix produced.** The `#### incomplete` rate here is 4/6.

### Does the churn predict more? **Yes — and the shape is predictable.**

- **5 defects live at HEAD** (`:302`, `:542`, `:583`, `:611` `ga_hash` unguarded recursion,
  CPY-0002 `_Py_make_parameters`), i.e. *more open than the file has had fixed in 18 months*.
- The rate is accelerating: 1 (2025 H2) → 5 (2026, 7 months).
- Strongest in-sample co-change coupling is `genericaliasobject.c` ↔ `unionobject.c` (5 co-fixes),
  and `unionobject.c:332,349` call straight into `_Py_subs_parameters`/`_Py_make_parameters`,
  so every invariant above is reachable via `typing.Union[…]` as well as `list[…]`.

**Prediction:** the next defect lands in `_Py_subs_parameters` or `subs_tvars` and violates
invariant 1 or 2. The intervention that breaks the cycle is not another point fix — it is a
comment block at `:160` stating the four invariants, plus `assert(x == NULL)` after every
`tuple_extend` call site (the idiom already used at `:554`).

---

## 3. Similar-bug propagation — whole tree

Two shapes, swept over every `.c`/`.h` in the repo (excluding vendored `libmpdec`/`expat`/
`_hacl`/`zlib` and generated `clinic/`). Scripts in
`…/scratchpad/gha/{shapeA,shapeB,shapeB3,shapeB4,uninit,hashclear}.py`.

### Shape B — `Py_DECREF` of a provably-NULL out-param

| pass | formulation | hits |
|---|---|---|
| B-1 | `_Py*_Resize(&v)` … `Py_DECREF(v)` within 14 lines | 2 (both FP) |
| B-2 | widened to 10 lookup APIs, line-window | 131 (unusable — success-branch DECREFs) |
| B-3 | brace-scoped: DECREF **inside** the `if (API(&v) < 0) { … }` block | **0** |
| B-4 | helper-mediated: local `f(PyObject **)` forwarding a resize, decoupled rc check | 0 (regex missed the multi-line signature) |

**Result: the direct form is clean tree-wide.** The two B-1 hits are correct code —
`Modules/_pickle.c:1423` DECREFs on a *different* branch, and `Objects/structseq.c:531` carries
`assert(keys == NULL); return -1;` (a guarded twin worth citing in the briefing).

The live instances are only reachable through a **local helper**, which is why every mechanical
formulation misses them. Only three helpers in the whole tree forward a NULLing resize through
a `PyObject **` parameter:

| helper | forwards | call sites | status |
|---|---|---|---|
| `Objects/bytesobject.c:3275` `PyBytes_Concat` | `_PyBytes_Resize(pv, …)` | public API | **safe** — uses `Py_CLEAR(*pv)` on every error path (guarded twin) |
| `Modules/zlibmodule.c:1422` `arrange_output_buffer_with_maximum` | `_PyBytes_Resize(buffer, …)` | 1 (`:1502`) | **safe** — caller uses `Py_CLEAR(return_value)` |
| `Objects/genericaliasobject.c:170` `tuple_extend` | `_PyTuple_Resize(dst, …)` | 2 | `:549` safe (`assert(newargs == NULL)`); **`:297` is the bug (V-3)** |

**Verdict: `genericaliasobject.c:302` is the only live instance of Shape B in CPython.** A
confident clean negative for the rest of the tree.

### Shape A — borrowed snapshot across a Python-reaching call, then DECREF of the stale local

Fingerprint: `local = obj->FIELD;` … Python-reaching call … `obj->FIELD = NULL;` `Py_DECREF(local);`

**Tree-wide hits: 1 — `Objects/iterobject.c:80`.** Every other `tp_iternext` that can run
arbitrary Python either reads the field afresh or uses `Py_CLEAR`:

- `Objects/enumobject.c:458` `reversed_next` — `Py_CLEAR(ro->seq)` + FT opt-out (V-1's twin).
- `Objects/iterobject.c:243,249` `calliter_iternext` — `Py_CLEAR` ×2.
- All `PySequence_GetItem` call sites tree-wide (30, enumerated) are either non-iterator
  argument-conversion loops or read the container from a local the callee cannot reach.

**Bounded negative:** the gh-101765 sweep's other 7 files (`bytearrayobject.c`, `bytesobject.c`,
`genericaliasobject.c`, `listobject.c`, `tupleobject.c`, `unicodeobject.c`) hold *concrete*-sequence
iterators using `PyList_GET_ITEM`-class macros, which cannot run Python. `seqiterobject` is the
only generic one that survived the sweep unfixed.

### Bonus shape — `dealloc-of-uninitialized-object`, whole tree

Since V-7 and V-8 are both this shape, I swept it tree-wide (non-zeroing alloc → fallible call →
`Py_DECREF(obj)` while a member assigned *later* is still garbage). **7 hits, 3 already reported
by other agents.** Of the 4 outside `Objects/`:

#### **[FIX] `Modules/_elementtree.c:2377` · `create_elementiter` — NEW, tree-wide, HIGH confidence**

```c
it = PyObject_GC_New(ElementIterObject, st->ElementIter_Type);   /* :2367 non-zeroing */
it->sought_tag  = Py_NewRef(tag);
it->gettext     = gettext;
it->root_element = (ElementObject*)Py_NewRef(self);
it->parent_stack = PyMem_New(ParentLocator, INIT_PARENT_STACK_SIZE);
if (it->parent_stack == NULL) {
    Py_DECREF(it);                       /* :2377 */
    PyErr_NoMemory();
    return NULL;
}
it->parent_stack_used = 0;               /* :2381 — too late */
it->parent_stack_size = INIT_PARENT_STACK_SIZE;
```

`elementiter_dealloc` then runs:

```c
Py_ssize_t i = it->parent_stack_used;    /* GARBAGE */
…
while (i--)
    Py_XDECREF(it->parent_stack[i].parent);   /* NULL + garbage*sizeof → wild read */
```

`it->parent_stack` is NULL, so the loop indexes off a NULL base with an uninitialised count and
`Py_XDECREF`s whatever it reads — an arbitrary refcount decrement, strictly worse than a NULL
deref. Reachable from Python via `Element.iter()` / `Element.itertext()` under memory pressure
(OOM class O5). **Guarded twin:** `Objects/dictobject.c:5644` `dictiter_new` — fallible call last;
or `Objects/templateobject.c` post-gh-151815. **Same shape as V-7/V-8, different directory,
never reported.**

#### Dismissed (my scanner's FP class — "garbage member never read on the dealloc path")

- `Modules/_decimal/_decimal.c:1456` — `modstate`/`tstate` garbage at `Py_DECREF(self)`, but
  `context_clear` only touches `traps`/`flags` and `context_dealloc` never calls
  `get_module_state_from_ctx`. **ACCEPTABLE.** Historically interesting: the 2012 author wrote
  `self->flags = NULL;` before the DECREF — a hand-rolled guard for exactly this hazard — and
  `fb0d9b9ac1e` (2023-06-29, gh-106078 heap-type migration) added `modstate`/`tstate` after
  both error paths without extending it. One `context_clear` line away from becoming V-7.
- `Modules/_sre/sre.c:1818` (`isbytes`), `:2987` (`pos`/`endpos`/`lastindex`) — scalars only;
  `match_clear`/`pattern_clear` touch pointers that are already set. **ACCEPTABLE.**

### Bonus shape — unfiltered `PyErr_Clear()` after `PyObject_Hash` (V-5's shape), whole tree

5 sites. 2 guarded (`Objects/setobject.c:2559`, `:2619` — filter on `PyExc_TypeError`), 3 unfiltered:

- `Objects/unionobject.c:170` — V-5, the finding.
- `Objects/codeobject.c:3210` — documented POLICY (gh-130851 comment: user-supplied
  `types.CodeType` constants may be unhashable, fall back to the address). Still swallows
  `MemoryError`/`KeyboardInterrupt`, but the intent is recorded. **POLICY.**
- **[CONSIDER] `Modules/_remote_debugging/binary_io_writer.c:290` `string_hash_func`** — NEW,
  outside `Objects/`. `PyObject_Hash(str)` failing → `PyErr_Clear(); return 0;`. If the argument
  is genuinely always an exact `str`, the hash cannot fail and the clear is dead code that hides
  a future invariant break; if it can be a subclass with `__hash__`, it silently collapses the
  hash table to bucket 0 **and** swallows the exception. Low severity, low reachability
  (internal hash table); reported for completeness.

---

## 4. Churn × quality matrix

Lifetime commits and `churn_rate` from the 9,203-commit run; *crash-fixes ≥2024* from the
Phase-1 digest; *live defects* = confirmed findings at HEAD from all Group A–D reports.

| # | file | commits | churn | fix-commits | crash-fixes ≥2024 | live defects | risk | action |
|---|---|---|---|---|---|---|---|---|
| 1 | `genericaliasobject.c` | 64 | 1.89 | 29 | **10** | **5** | **HIGHEST** | invariant comment + `assert` sweep; V-3 first |
| 2 | `iterobject.c` | 78 | 4.34 | 30 | 4 | 2 | **HIGHEST** | V-1 — `Py_CLEAR`; **zero FT commits ever** |
| 3 | `structseq.c` | 118 | **5.21** | 37 | 6 | 4 | **HIGHEST** | V-6 — bound `get_type_attr_as_size` at `:28` |
| 4 | `odictobject.c` | 126 | 3.31 | 65 | 12 | 2 | HIGH | V-7 + the 11-year reentrancy cluster (`odict_repr`) |
| 5 | `tupleobject.c` | **287** | **6.35** | **96** | 13 | 2 | HIGH | V-9 — highest churn + highest fan-in (277 files) |
| 6 | `unionobject.c` | 53 | 3.46 | 21 | 6 | 3 | HIGH | V-5 — **17 months untouched**, latent |
| 7 | `descrobject.c` | 224 | 4.43 | 74 | 8 | 1 | HIGH | TSAN-0043 now has a 7-day-old guarded twin |
| 8 | `weakrefobject.c` | 132 | 3.03 | 44 | 7 | 0 | MODERATE | historically the *fixed* class; gh-121652 complete |
| 9 | `funcobject.c` | 244 | 4.17 | 73 | 12 | 1 | MODERATE | init-bypass sweep never followed gh-144330 |
| 10 | `templateobject.c` | 6 | 1.41 | 2 | 2 | 1 | MODERATE | new code (3.14); 2 of 6 commits are crash fixes |

Below the cut, one line each: `cellobject.c` 39/4.72/8 and `capsule.c` 19/1.73/6 are **dormant**
(0 commits in 18 months) — low churn, low complexity, **LOW risk, deprioritize**.
`interpolationobject.c` 4/1.03/0 and `lazyimportobject.c` 2/1.01/1 are **new and unreviewed**
rather than stable — low churn is *absence of scrutiny*, not evidence of correctness;
`lazyimportobject.c` has never had a bug-fix commit.

**Concentration: SPREAD, and the spread is the finding.** The top-3 risk files rank 30th, 24th
and 19th by raw churn in `Objects/`. Raw churn ranks `unicodeobject.c` (1,641) and
`typeobject.c` (1,200) first; neither is in the sample and neither carries a live defect here.
**Raw churn is anti-correlated with defect density in this sample** — `genericaliasobject.c` has
5 live defects at 64 lifetime commits while `descrobject.c` has 1 at 224. The predictive signal
is *crash-class fixes since 2024 per KLOC*, not commits.

Two cells of the brief's matrix are populated by V-5 and V-8:
- **Low churn + known bug pattern = HIGH (latent).** `unionobject.c:172` — 17 months, zero commits.
- **High churn + known bug pattern = HIGHEST.** `genericaliasobject.c` — 4 of 6 recent commits incomplete.

---

## 5. New siblings of known shapes

The fix-propagation yield of this agent, ranked by confidence:

1. **[FIX, HIGH]** `Modules/_elementtree.c:2377` — `dealloc-of-uninitialized-object`, tree-wide
   sibling of V-7/V-8. Garbage `parent_stack_used` drives a `Py_XDECREF` loop over a NULL base.
2. **[FIX, HIGH]** `Objects/enumobject.c:456-459` is the *guarded twin* that closes V-1 — the
   patch for `iter_iternext` is `Py_CLEAR(it->it_seq)`, already written and shipped for
   `reversed_next` in 2025-03.
3. **[FIX, HIGH]** `Objects/setobject.c:2559` is the *guarded twin* that closes V-5 — filter on
   `PyErr_ExceptionMatches(PyExc_TypeError)` before clearing.
4. **[FIX, MEDIUM]** `Objects/genericaliasobject.c:583` `ga_getitem` — second `alias->parameters`
   write site missed by `68abf17fa92` seven days before HEAD (V-4; independently confirmed by
   ft-race-scanner #1 and refcount-auditor #3).
5. **[CONSIDER, LOW]** `Modules/_remote_debugging/binary_io_writer.c:290` — unfiltered
   `PyErr_Clear` after `PyObject_Hash`.
6. **[CONSIDER, MEDIUM]** `Modules/_decimal/_decimal.c:1456` — a 2012 hand-rolled uninit guard
   that a 2023 heap-type migration silently outgrew. Currently harmless; one `context_clear`
   line from becoming a crash. Worth a comment, not a patch.

---

## 6. Classes bounded (clean negatives)

- **Shape B (`Py_DECREF` of a NULLed out-param) is clean tree-wide outside
  `genericaliasobject.c:302`.** Four independent formulations; the only three helpers in CPython
  that forward a NULLing resize through `PyObject **` are enumerated above and two are correct.
- **Shape A (clear-then-DECREF-a-stale-local) has exactly one instance tree-wide**
  (`iterobject.c:80`). All 30 `PySequence_GetItem` call sites triaged; all other
  Python-reaching `tp_iternext` slots read the field afresh or use `Py_CLEAR`.
- **gh-121652 (`allocate_weakref` NULL checks) is complete** — re-verified at HEAD, both call
  sites (`weakrefobject.c:440`, `:451`) guarded. Concurs with git-history-context L-8; do not re-chase.
- **`Modules/_sre/sre.c:1818` and `:2987`, `Modules/_decimal/_decimal.c:1456`** are *not*
  uninit-dealloc bugs — the garbage members are scalars or are never read by `tp_clear`.

---

## 7. Toolkit assessment ← REQUIRED

### Does `analyze_history.py` now support fix-completeness work?

**It unblocks it; it does not support it.** The `UnicodeDecodeError` fix is load-bearing —
without a >10-year window, none of V-1 (2016/2023/2025), V-8 (2024) or V-9 (2026) is visible,
and `iterobject.c`'s decisive fact (*zero* FT commits ever) is unprovable. 9,203 commits in
~11 s is comfortably fast enough for routine use.

But **every one of the nine verdicts above was reached with `git show` and `git blame`, not with
the script's output.** The script gave me the corpus and the churn table; it did not answer a
single fix-completeness question. Concretely:

| what fix-completeness needs | script provides | I used instead |
|---|---|---|
| "which commit introduced this line?" | — | `git blame -L` (9×) |
| "what else did that commit touch?" | `files` list, no diff scoping | `git show --stat` |
| "did the fix cover all sites in the function?" | — | manual read |
| "was this file ever touched for FT?" | — | `git log --since -- <file>` |
| bug-fix density per file | `commits` only | Phase-1 digest's hand-rolled `--follow` counts |

### Precision — the `fix` classifier

**4,133 of 9,203 commits (44.9%) are classified `fix`.** That is not a classifier, it is a
coin flip, and it is the single biggest defect. It matches keywords anywhere in the message, so
it swallows the 2022 `Py_NewRef` hygiene series, the `gh-111178` signature sweep, the PEP-585
docs pass, and the pre-2009 SVN mega-merges. `recent_fixes` is 4,133 entries with full diffs —
13.4 MB of JSON, of which the nine commits that mattered were found by grepping for issue
numbers I already had.

The `unknown` bucket (1,949, 21%) is where `a933e9ccee6` (V-7's near-miss, *"Use
`_PyTuple_FromPair[Steal]` in Objects"*) and `17d31bf3843` (V-8's introduction, *"Store memory
allocation information into `_PyListArray`"*) both live. **Two of the three commits that
*introduced* a confirmed defect are classified `unknown`, and the third (`8060aa5d7dd`,
*"Fix various refleaks"*) is classified `fix`.** The classifier's sign is inverted for the
highest-value cases.

### Recall gaps — what I found by reading that the script cannot surface

1. **The `#ifdef`-arm asymmetry (V-8).** A `Py_DECREF` in a `#ifdef Py_GIL_DISABLED` arm whose
   `#else` arm orders the same statements safely. No churn or keyword signal; only a diff read.
2. **The formalised copy-paste (V-9).** `244300162d2` *added the comment naming the duplicate*.
   A comment saying "update also X which copied this code" is a machine-readable
   fix-propagation edge and nothing consumes it.
3. **The single-error-class sweep (V-2, V-3).** `8060aa5d7dd` swept refleaks across 7 files and
   introduced a NULL deref. The signal — "a commit whose message names one error class edited a
   function containing a different error class" — needs the diff, which the script has and
   never analyses.
4. **The `--no-function` cliff.** I had to pass it (the regex function-churn pass is the slow
   part). Function-level churn is exactly what V-2/V-3/V-4 need — three defects in one
   1,070-line file, all in three functions. `function_churn: []` with the note
   `"Function-level churn skipped"` is the wrong trade: file-level churn ranked
   `genericaliasobject.c` 30th while function-level would have ranked `_Py_subs_parameters` #1.

### Prompt issues

- The agent brief's CPython-specific section (Argument Clinic, `PyModule_AddObjectRef`, stable
  ABI) is **dead weight for `Objects/`**. I checked: zero Clinic-migration and zero
  `PyModule_AddObject` findings in scope. Those analyses belong to `Modules/`, and the brief
  should gate them on scan root.
- "Cap at 10 similar-bug findings" is the wrong instrument here. My highest-value results are
  **three bounded negatives** ("Shape B is clean tree-wide except one site"), which the output
  template has no slot for. The brief should require a *sweep-completeness* statement per shape.
- The `module_families` field (`hash`, `dbm`, `io`, `codec`, `sqlite`, `ctypes`, `zstd`) is
  `Modules/`-shaped and empty of signal for `Objects/`. The real families here are the
  include-graph-mapper's F1–F8, which the script cannot see.

### Concrete tuning proposals (ranked by value)

**T-1 — Add `--introduced-by <file:line>` (highest value).** One flag that runs
`git log -L<line>,<line>:<file>` and prints the introducing commit, its message, its full
file list, and whether its type is `fix`. This is the primitive every fix-completeness verdict
needs and I ran it by hand nine times. Roughly 30 lines wrapping `git log -L`.

**T-2 — Fix the `fix` classifier: score, don't match.** Replace substring matching with a
weighted score, and *demote* on hygiene markers:
```python
FIX_STRONG   = (r"\bgh-\d+\b.*\b(crash|segfault|use.after.free|double.free|"
                r"refleak|leak|NULL deref|data race)\b", +3)
FIX_WEAK     = (r"\b(fix|bug)\b", +1)
DEMOTE       = (r"\b(Py_NewRef|Py_SETREF|signature|docstring|typo|whatsnew|"
                r"PEP.585|Argument Clinic|Remove (unused|redundant))\b", -3)
```
Emit `fix_confidence: high|medium|low` and split `recent_fixes` by it. Target: `high` ≤ 8% of
commits. Also emit `crash_class: bool` from the briefing's six shape names so
`recent_fixes[crash_class]` is directly the watchlist.

**T-3 — Per-file bug-fix density with `--follow`.** Add to each `file_churn` entry:
`bug_fix_commits`, `crash_fix_commits`, `crash_fixes_since(YYYY)`, and `follow_renames: true`.
The Phase-1 digest computed these by hand and they produced a **strictly better ranking than
`churn_rate`** — `genericaliasobject.c` is 30th by churn and 1st by crash-fix recency, and it
holds 5 of the sample's live defects. Add a derived `risk_score = crash_fixes_since_2024 / KLOC`.

**T-4 — Mine "copied from" / "update also" comments into a `duplication_edges` list.**
`git grep -nE '(Code )?copied from|update also .*which copied'` yields
`tupleobject.c:365 → dictobject.c:8427` (V-9) directly. In-tree, maintainer-authored,
zero false positives, and it is exactly a fix-propagation edge. ~15 lines.

**T-5 — Emit `single_class_sweep` on multi-file `fix` commits.** When a `fix` commit touches
≥4 files and its message names one error class (`refleak`, `NULL`, `signature`), flag it:
these are the commits most likely to have *introduced* a different class (V-3) or missed a
sibling (V-2). Two of three introducing commits in this run are multi-file sweeps.

**T-6 — Make `--no-function` unnecessary.** Cache the tree-sitter parse per blob instead of
re-parsing per commit, or add `--function-scope <file>` to compute function churn for a
handful of files at full history depth. Function churn is the deliverable for a
three-defects-in-one-file case and it is currently the thing users are advised to switch off.

**T-7 — Data-file entry.** Add to `data/cpython_non_bugs.md`, under *Uninitialized dealloc*:
> **Garbage scalar members are not a bug.** A member left uninitialised at an early
> `Py_DECREF` matters only if `tp_clear`/`tp_dealloc` *reads* it. `Py_ssize_t`/flag members
> that no cleanup path touches are ACCEPTABLE (`_sre/sre.c:1818`, `:2987`,
> `_decimal/_decimal.c:1456`). The bug is a garbage member that is dereferenced, freed, or
> used as a loop bound (`_elementtree.c:2377`).

This FP class was 3 of my 4 non-`Objects/` uninit-dealloc hits.
