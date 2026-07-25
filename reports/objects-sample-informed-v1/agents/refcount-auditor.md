# refcount-auditor — Objects/ sample (informed)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Differential binary:** `/home/danzin/projects/cpython/python` — **ASan + `--with-pydebug`** build at
`583cb03da83` (2026-05-25). All four findings below were reproduced on it; line numbers were
re-checked against HEAD by hand and agree.

## Scanner volume

```
raw candidates in sample: 19   |   confirmed: 0   |   dismissed: 19   |   precision: 0%
```

Every one of the four real findings in this report was found **by reading**, not by
`scan_refcounts.py`. Two of them are heap-use-after-free reproduced under ASan from pure Python.
The scanner has **no borrowed-ref-across-call rule at all** (see Toolkit assessment) — the crown-jewel
shape it is nominally credited with in the briefing is not implemented.

---

## Findings

### FIX

#### 1. `Objects/genericaliasobject.c:542` · `_Py_subs_parameters` · heap-use-after-free (ASan-confirmed)

```c
537        if (unpack) {
538            if (!PyTuple_Check(arg)) {
539                Py_DECREF(newargs);
540                Py_DECREF(item);
541                Py_XDECREF(tuple_args);                      /* frees the tuple ... */
542                PyObject *original = PyTuple_GET_ITEM(args, iarg);   /* ... then reads it */
543                PyErr_Format(PyExc_TypeError,
544                             "expected __typing_subst__ of %T objects to return a tuple, not %T",
545                             original, arg);
```

**What breaks.** 82 lines earlier the function does `args = tuple_args = PySequence_Tuple(args);`
when the incoming `args` is a *list* (genericaliasobject.c:460). `args` and `tuple_args` are then
the **same object**, and `tuple_args` holds its only reference. `Py_XDECREF(tuple_args)` at :541 frees
the tuple; `PyTuple_GET_ITEM(args, iarg)` at :542 reads its storage; `PyErr_Format("%T", original)`
then dereferences whatever came back as a type pointer.

**Guarded twin.** Every *other* `Py_XDECREF(tuple_args)` in the file (`:470 :486 :500 :512 :519 :534
:554 :566`, and all eight in `_Py_make_parameters` at `:201 :214 :225 :234 :245 :260 :264`) is
immediately followed by `return`. `:541` is the single site that touches `args` afterwards. The fix is
to hoist the load above the DECREF block, exactly as the surrounding code already does.

**Python-level reach.** `args` is a list whenever `_Py_subs_parameters` recurses on a PEP-612-style
bracketed parameter list (`genericaliasobject.c:482`, the `t = list[[T]]` case named in the comment at
:454). Trigger needs a duck-typed `__typing_is_unpacked_typevartuple__` plus a `__typing_subst__` that
returns a non-tuple — both are plain attribute lookups (`_is_unpacked_typevartuple`, :325-337), so a
pure-Python class suffices:

```python
from types import GenericAlias
class TV:
    __typing_is_unpacked_typevartuple__ = True
    def __typing_subst__(self, arg): return 42          # non-tuple
tvs = [TV() for _ in range(30)]      # >20 so the tuple bypasses the tuple freelist
GenericAlias(list, (list(tvs),))[tuple(range(30))]
```

```
ERROR: AddressSanitizer: heap-use-after-free ... READ of size 8
    #0 _Py_TYPE_impl Include/object.h:234
    #1 _Py_subs_parameters Objects/genericaliasobject.c:542
    #2 _Py_subs_parameters Objects/genericaliasobject.c:482
    #3 ga_getitem        Objects/genericaliasobject.c:589
freed by thread T0 here:
    #2 _Py_subs_parameters Objects/genericaliasobject.c:541
```

With ≤20 elements the freed tuple lands on the tuple freelist, whose link word overwrites `ob_refcnt`
rather than `ob_item[0]` — so small cases read stale-but-intact memory and look fine. That is why this
survived: it is invisible without ASan below the freelist threshold.

**Provenance.** Introduced by `1da989be74e` (gh-138479, "Ensure that `__typing_subst__` returns a
tuple", Sept 2025) — the whole `if (!PyTuple_Check(arg))` block is that commit. Phase 1 lead #2
(`_Py_subs_parameters` fixed twice, sweep `8060aa5d7dd` incomplete) is confirmed and extended: the
sweep added a `Py_DECREF(item)` at :460 and did not look at :542.

**Classification:** FIX. Read of freed memory on a pure-Python-reachable path; a `%T` on the recycled
slot is a type-confused dereference.

---

#### 2. `Objects/iterobject.c:80` · `iter_iternext` · double-DECREF → use-after-free (ASan-confirmed)

```c
61       seq = it->it_seq;                                  /* borrowed slot load */
62       if (seq == NULL) return NULL;
...
70       result = PySequence_GetItem(seq, it->it_index);    /* arbitrary Python: __getitem__ */
71       if (result != NULL) { it->it_index++; return result; }
75       if (PyErr_ExceptionMatches(PyExc_IndexError) ||
76           PyErr_ExceptionMatches(PyExc_StopIteration))
77       {
78           PyErr_Clear();
79           it->it_seq = NULL;
80           Py_DECREF(seq);                                /* stale pointer, unconditional */
81       }
```

**What breaks.** `seq` is loaded from the slot *before* `PySequence_GetItem` runs user code. If that
user code re-enters `next()` on the same iterator, the nested `iter_iternext` takes the exhaustion
branch itself: it sets `it->it_seq = NULL` and drops the iterator's single reference. When the outer
frame reaches :79-80, `it->it_seq` is already NULL, but `Py_DECREF(seq)` fires anyway on the *stale
local* — the one reference is released twice. The sequence is freed while still reachable from Python.

**Guarded twin.** `calliter_iternext`, ~165 lines below in the same file (`iterobject.c:243-244`,
`:249-250`), does the identical teardown with `Py_CLEAR(it->it_callable); Py_CLEAR(it->it_sentinel);`
— `Py_CLEAR` re-reads the field, NULLs it, and only then DECREFs, so a nested clear is a no-op the
second time. `iter_iternext` open-codes the same idiom incorrectly. The one-line fix is
`Py_CLEAR(it->it_seq);` in place of :79-80. The file's authors are already aware of this class:
`iter_reduce` (:114-117) and `calliter_reduce` (:261-264) both carry the comment *"`_PyEval_GetBuiltin` can
invoke arbitrary code, call must be before access of iterator pointers, see issue #101765."*

**Python-level reach.** `iter(obj)` on any object with `__getitem__` but no `__iter__` produces a
`seqiterobject`:

```python
class S:
    def __init__(self): self.n = 0
    def __getitem__(self, i):
        self.n += 1
        if self.n == 1:
            next(it)            # re-entrant; inner call exhausts and DECREFs
        raise IndexError
s = S(); it = iter(s)
next(it)                        # outer call DECREFs the same reference again
```

(The inner `next(it)` raises `StopIteration` out of `__getitem__`, and :76 accepts `StopIteration`
as an exhaustion signal — so the outer DECREF always fires.)

```
ERROR: AddressSanitizer: heap-use-after-free ... READ of size 4
    #0 Py_INCREF Include/refcount.h:286
    #2 _PyDict_GetItemRef_KnownHash Objects/dictobject.c:2522
    #4 _PyEval_LoadName Python/ceval.c:3720          <- reading the global `s` afterwards
freed by thread T0 here:
    #1 subtype_dealloc Objects/typeobject.c:2871
    #4 iter_iternext   Objects/iterobject.c:80
```

**Classification:** FIX. This is the crown-jewel `borrowed-ref-across-call` shape in its purest form:
a borrowed slot pointer used to release ownership after a call that can run arbitrary Python. Segfault
is reachable from three lines of Python with no C extension involved.

---

### CONSIDER

#### 3. `Objects/genericaliasobject.c:583` · `ga_getitem` · unfixed sibling of a 7-day-old FT fix, plus a reentrancy leak

```c
578  static PyObject *
579  ga_getitem(PyObject *self, PyObject *item)
580  {
581      gaobject *alias = (gaobject *)self;
582      // Populate __parameters__ if needed.
583      if (alias->parameters == NULL) {
584          alias->parameters = _Py_make_parameters(alias->args);   /* runs Python; raw store */
```

**Guarded twin.** `68abf17fa92` (gh-153298, **7 days before HEAD**) split `ga_parameters` into
`ga_parameters_lock_held` + a `Py_BEGIN_CRITICAL_SECTION` wrapper precisely because this lazy
initialisation races on free-threaded builds. `unionobject.c:327-339` (`union_init_parameters`) has
the same wrapper. `ga_getitem:583` performs the **identical** lazy init with no critical section and
was not touched by that commit — confirmed by `git show 68abf17fa92 -- Objects/genericaliasobject.c`,
whose entire diff is inside `ga_parameters`. Phase 1 lead #1 said TSAN-0043's twin was a week old;
the twin also has a second unfixed sibling in its own file.

**Second defect, present in all three sites (`ga_getitem:584`, `ga_parameters_lock_held:848`,
`union_init_parameters:332`):** the store is a *raw* assignment, not `Py_XSETREF`. `_Py_make_parameters`
calls `PyObject_HasAttrWithError(t, __typing_subst__)` (`:211`) — arbitrary Python. If that attribute
lookup re-enters `__parameters__`, the inner call computes and stores tuple A; the outer then
overwrites the field with tuple B and **never DECREFs A**. `Py_BEGIN_CRITICAL_SECTION` does not help:
it compiles to nothing in the default GIL build, and single-thread reentrancy is not a race.
Measured on the debug build:

```python
class T:
    @property
    def __typing_subst__(self):
        if not done: done = True; ga.__parameters__     # re-enter
        return lambda arg: arg
# control  refcnt(t): 4
# reentrant refcnt(t): 5      <- the inner parameters tuple leaked, still holding t
```

**Classification:** CONSIDER — the FT race is a fix-propagation gap (mechanical, low risk); the
reentrancy leak is a real but unbounded-only-by-attacker-effort leak, not a crash. Both are fixed
together by `Py_XSETREF` inside a critical section, and by routing `ga_getitem` through
`ga_parameters`.

---

#### 4. `Objects/descrobject.c:624` · `descr_get_qualname` · reentrancy leak (TSAN-0043's refcount half)

```c
621  descr_get_qualname(PyObject *self, void *Py_UNUSED(ignored))
622  {
623      PyDescrObject *descr = (PyDescrObject *)self;
624      if (descr->d_qualname == NULL)
625          descr->d_qualname = calculate_qualname(descr);      /* runs Python; raw store */
626      return Py_XNewRef(descr->d_qualname);
```

`calculate_qualname` (:592-617) does `PyObject_GetAttr((PyObject *)descr->d_type, &_Py_ID(__qualname__))`
— arbitrary Python whenever the owning class has a metaclass with `__getattribute__`. Same shape as
finding 3: no `Py_XSETREF`, no critical section. Reproduced on the debug build with 200 descriptors:

```
no-reentrancy delta:  202 total refs
   reentrancy delta:  603 total refs      (+2 per descriptor)
```

**Guarded twin:** `ga_parameters` (`genericaliasobject.c:856-864`) for the locking half; `Py_XSETREF`
usage throughout `descrobject.c` itself (`property_init_impl:1862-1866`, `property_set_name:1632`) for
the store half.

**Classification:** CONSIDER. Confirms Phase 1 lead #1 from the refcount side — TSAN-0043 is not only
a data race, it leaks under plain single-threaded reentrancy too.

---

### POLICY / ACCEPTABLE (one line each)

- `weakrefobject.c:1085-1087` — `PyTuple_GET_ITEM(tuple, i)` borrowed across `handle_callback()`:
  **ACCEPTABLE**, `tuple` is a function-local strong owner that no callback can name (textbook
  "borrowed under a known-live owner"). This was the briefing's flagship candidate site; it is clean.
- `weakrefobject.c:977-988` — `PyWeakref_GetObject` DECREFs then returns the pointer: **POLICY**,
  documented borrowed-return API, removed in 3.15, kept only for stable-ABI compatibility.
- `odictobject.c:1256-1278` — `OrderedDict_copy_impl` passes the borrowed `PyODict_GetItemWithError`
  result into `_PyODict_SetItem_KnownHash_LockHeld`: **ACCEPTABLE**, `insertdict` INCREFs key and
  value before the lookup that can call `__eq__`.
- `odictobject.c:1842-1848` — `Py_DECREF(PyTuple_GET_ITEM(result, 0))` on the recycled iterator result
  tuple: **ACCEPTABLE**, byte-for-byte the established `dictiter_iternextitem` idiom; the
  `_PyObject_IsUniquelyReferenced` gate makes a re-entrant `next()` take the allocating branch.
- `odictobject.c:2362-2366` — `mutablemapping_update` `Py_INCREF(other)` before use: this is the
  **guarded twin** for the whole borrowed-arg class in that file.
- `tupleobject.c:419` (`tuple_contains`) — borrowed item across `PyObject_RichCompareBool`:
  **ACCEPTABLE**, tuples are immutable and the owner is alive.
- `unionobject.c:56-58, 86-101` — `alias->unhashable_args` re-read across `PyObject_Hash` /
  `PySequence_Contains`: **ACCEPTABLE**, union fields are `Py_READONLY` and never reassigned after
  construction.
- `descrobject.c:504-509, 553-565, 1630-1632` — `PyTuple_GET_ITEM(args, …)` held across
  `classmethod_get` / `_PyObject_RealIsSubclass` / `Py_XSETREF`: **ACCEPTABLE**, `args` is the
  caller's tuple; `_PyObject_RealIsSubclass` on two real types reduces to `PyType_IsSubtype`.
- `funcobject.c:555-574` — `ann_tuple` borrowed from `op->func_annotations` across `PyDict_SetItem`:
  **ACCEPTABLE today, fragile**. Safe only because the tuple form is emitted exclusively by the
  compiler (`Python/codegen.c:1005`, `MAKE_FUNCTION_ANNOTATIONS`) with interned `str` keys, so
  `insertdict` never calls Python. `function___annotations___set_impl:915` rejects non-dicts, so no
  Python path can plant an arbitrary-key tuple. One `Py_INCREF(ann_tuple)` would make it
  unconditionally safe.
- `structseq.c:234-236`, `templateobject.c:149-158, 254-257` — borrowed items across
  `PyUnicode_Concat` / `Py_NewRef`: **ACCEPTABLE**, no Python-reaching call in between.

---

## New siblings of known shapes

| # | shape | guarded twin (the fix) | unfixed sibling found | status |
|---|---|---|---|---|
| 1 | `borrowed-ref-across-call` (owner freed before the borrowed read) | every other `Py_XDECREF(tuple_args)` in `genericaliasobject.c`, each followed by `return` | `_Py_subs_parameters` `genericaliasobject.c:541→542` | **FIX**, ASan-confirmed |
| 2 | `borrowed-ref-across-call` (stale slot load released after a Python-reaching call) | `calliter_iternext` `iterobject.c:243,249` uses `Py_CLEAR` | `iter_iternext` `iterobject.c:61→80` | **FIX**, ASan-confirmed |
| 3 | lazy-init under a critical section (gh-153298, 7 days old) | `ga_parameters` `genericaliasobject.c:857-864`; `union_init_parameters` `unionobject.c:327-339` | `ga_getitem` `genericaliasobject.c:583` — no critical section | **CONSIDER** |
| 4 | lazy-init raw store (no `Py_XSETREF`) → reentrancy leak | `Py_XSETREF` in `property_init_impl` `descrobject.c:1862` | `descr_get_qualname:624`, `ga_parameters_lock_held:848`, `ga_getitem:584`, `union_init_parameters:332` | **CONSIDER**, leak measured |

**Scope escapes** (same lazy-init raw-store shape, outside the 14-file sample — for whoever takes the
next slice): `Objects/bytearrayobject.c:924` (`ob_bytes_object`), `Objects/enumobject.c:181`
(`en_longindex`), `Objects/typeobject.c:9479` (`ht_cached_keys`). Not triaged here.

---

## Classes bounded (clean negatives)

- **`Objects/odictobject.c` reentrancy→UAF cluster is closed for the stale-node shape.**
  Phase 1 lead #3 said `odict_repr` still lacks an `od_state` snapshot. It does — but it no longer
  needs one: current `odict_repr` (`:1439-1465`) does not walk the linked list at all; it
  `Py_ReprEnter`s, takes `PyDict_Copy(self)` and formats the *copy* with `%R`. There is no node
  pointer held across user code. Every remaining LL walk that runs Python **does** snapshot:
  `_odict_keys_equal:824-825,847`, `OrderedDict_copy_impl:1254,1274,1291`, `odictiter_nextkey:1752`,
  `odictiter_iter:1964`. The 2015/2024/2026 cluster looks genuinely swept.
- **`Objects/weakrefobject.c` is clean for this bug class.** The briefing called it "the textbook
  setting for the crown jewel". It is not, and the reason is structural: the file uses
  `_PyWeakref_GET_REF` (a *strong*-ref accessor) everywhere — 17 references, every one `Py_DECREF`d — and
  the one callback-invocation loop (`PyObject_ClearWeakRefs:1084-1090`) reads from a private local
  tuple built under `LOCK_WEAKREFS` with `_Py_TryIncref` (`:1069`). I found no borrowed reference
  crossing a callback anywhere in the file.
- **`Objects/tupleobject.c` is clean for this bug class** (277-file fan-in, so worth stating): every
  `PyTuple_GET_ITEM` that feeds a Python-reaching call (`:419` `tuple_contains`, `:854` `tuple_new`,
  `:1142` `tupleiter_next`) reads from an immutable container owned by a live caller.
- **The stale-slot-load-then-DECREF shape has exactly one instance in all of `Objects/`.** I wrote a
  targeted matcher for it (load `x = obj->fld`, …Python-reaching call…, `obj->fld = NULL;
  Py_DECREF(x);`) and ran it over `Objects/`, `Python/`, `Modules/`, `Modules/_io`, `Modules/_sqlite`,
  `Modules/_ctypes`: **one true positive** (`iterobject.c`, finding 2) and one false positive
  (`Modules/_io/textio.c:1657→1705`, where the only intervening calls are `Py_DECREF` on a `bytes`).
  This is a cheap, high-precision rule — see tuning proposal T1.
- **`new_missing_member_init` is empty as a bug class in CPython** — see the Toolkit assessment; the
  premise it rests on does not hold in this interpreter.

---

## Toolkit assessment

### Precision — 0/19. Every candidate was a false positive.

| rule | n | FP class | root cause |
|---|---|---|---|
| `potential_leak` | 4 | **member-assignment mistaken for a local** | `_ASSIGN_CALL_RE` is `(\w+)\s*=\s*API\(`. On `ub->args = PyList_New(0)` it captures `args`; on `self->func_annotations = PyDict_New()` it captures `func_annotations`. Ownership went to a struct with its own finalizer. Sites: `unionobject.c:143,147,172`, `funcobject.c:884`. |
| `potential_leak` | 1 | **out-parameter through a dereference** | `*pv = PyTuple_New(newsize)` captures `pv`. Ownership transfers to the caller. Site: `tupleobject.c:1065`. |
| `potential_leak` | 2 | **`Py_SETREF`/`Py_XSETREF` second argument not modeled** | `_DECREF_RE` matches `Py_SETREF\s*\(\s*(\w+)` and only records the *first* argument. `Py_SETREF(item, tmp)` marks `item` consumed but leaves `tmp` looking leaked. Sites: `genericaliasobject.c:428`, `capsule.c:255`. |
| `potential_leak` | 2 | **ownership escapes through a helper call** | `return set_orig_class(obj, self);` — the var is neither `return`ed directly nor DECREF'd. Sites: `genericaliasobject.c:645,653`. |
| `potential_leak_on_error` | 2 | **flow-insensitive** | The rule fires when a returned var is absent from the error label's cleanup. In both cases the variable is only *assigned* after the last `goto`, so it is provably NULL on every error path. Sites: `odictobject.c:1011,1456`. |
| `potential_double_free` | 1 | **mutually exclusive branches** | `arg` is `PyTuple_SET_ITEM`-stolen in one branch and `Py_DECREF`'d in another; the rule intersects two flat sets. Site: `genericaliasobject.c:517`. |
| `new_missing_member_init` | 6 | **premise is wrong for CPython, twice over** | see below. |
| `init_not_reinit_safe` | 1 | **name-suffix matching, not slot registration** | `unionbuilder_init` is not a `tp_init` — `unionbuilder` is a plain C struct on the stack with `unionbuilder_finalize` as its destructor. Site: `unionobject.c:142`. |

**On `new_missing_member_init` specifically** (6 of 19 candidates, ~32% of the sample), the rule's
stated rationale — *"`object.__new__()` without `__init__()` will leave garbage pointers"* — is not how
CPython works, and I verified both halves on the build:

1. `object.__new__(T)` **never routes through `T`'s `tp_new`**. It allocates via `type->tp_alloc`,
   which zeroes. And when `tp_new` is overridden it refuses outright:
   `TypeError: object.__new__(string.templatelib.Interpolation) is not safe, use ...__new__()`.
2. Five of the six flagged functions (`PyCell_New`, `PyDictProxy_New`, `PyWrapper_New`,
   `PyCallIter_New`, `PyAnextAwaitable_New`) are **C-API constructors, not `tp_new` slots** at all.
   `_is_tp_new` matches any identifier ending in `_New`.
3. All six initialise every pointer member immediately after the allocation — with
   `Py_NewRef(x)`/`Py_XNewRef(x)`, not `= NULL`. `_build_member_null_init_re` only accepts
   `param->member = NULL;` or `= 0;`, so any correct constructor that stores a real value is flagged.

The genuine bug this rule is groping for is the briefing's `dealloc-of-uninitialized-object` shape,
which is `scan_uninit_dealloc.py`'s job and requires a *fallible call between the allocation and the
member stores*. None of the six has one.

### Recall gaps — the important half

**RG-1 (critical): the crown-jewel rule does not exist.** The briefing lists
`borrowed-ref-across-call` as *"surfaced by `scan_refcounts.py` (widen the Python-reaching call set)"*.
There is no set to widen. `BORROWED_REF_APIS` (scan_refcounts.py:125-141) is defined and folded into
`_ANY_API_CALL_RE` (:270-277) — and **`_ANY_API_CALL_RE` is never used**. Neither is
`_RETURN_NULL_RE` (:295), `INCREF_APIS` (:150), or the `increffed` set (:559-561, computed and
discarded). `analyze_function_refcounts` implements exactly four checks: unbalanced new ref,
new-ref-on-error, steal∩decref, and a `pass`-bodied dead block at :645-660. Both FIX findings in this
report are invisible to it by construction. This is the single highest-value gap.

**RG-2: no notion of "this pointer's owner died".** Finding 1 is not a leak or a double-free — it is a
*read* of a pointer whose owning container was released three lines above, through an **alias**
(`args = tuple_args = ...`). Nothing in the scanner tracks aliasing or orders DECREFs against later
uses.

**RG-3: no notion of "this local is stale".** Finding 2 is a `Py_DECREF(local)` where the slot the
local came from was re-NULLed by re-entrancy. Detectable syntactically without any dataflow: see T1.

**RG-4: lazy-init raw store is not modeled.** `x->f == NULL` → `x->f = fallible_call(...)` (findings 3
and 4) is both a reentrancy leak and, on FT builds, a data race. A four-line regex found 8 instances
across `Objects/` in one pass, 3 of which are in the sample. Neither `scan_refcounts.py` nor
`scan_ft_races.py` surfaced any of them in the sample (`scan_ft_races` returned 6 candidates, none at
these sites).

**RG-5: `Py_CLEAR`-omission is advertised but not implemented.** The module docstring (:4-5) claims the
scanner detects *"missing Py_XDECREF, and Py_CLEAR suggestions."* No such check exists. Finding 2 is
precisely a Py_CLEAR omission.

### Prompt issues

- The agent definition's Phase 3 devotes a third of its length to `init_not_reinit_safe` /
  `new_missing_member_init`. On CPython's own `Objects/` these produced 7 of 19 candidates and 0 real
  bugs, and the `object.__new__` rationale in both the prompt and the scanner's `detail` string is
  factually wrong for this codebase. It should be demoted to a footnote, or scoped to third-party
  extension review where types genuinely do expose `tp_new` to `object.__new__`.
- Phase 4's borrowed-reference bullet names only `PyList_GetItem` and suggests `PyList_GetItemRef`.
  Both real findings involve neither a list nor a container accessor: one is a struct-field load, the
  other an aliased local. The prompt should lead with **"a pointer read before a Python-reaching call
  and used after it"** — the container case is the narrow instance, not the shape.
- The prompt says "the script has a ~30-50% false positive rate". On this sample it was 100%. The
  stated rate sets the wrong expectation and discourages the reading that actually produced results.

### Concrete tuning proposals (ranked by value)

**T1. New finding type `stale_slot_decref` — highest value/effort ratio.** Implements RG-3; found
finding 2 with 1 FP tree-wide.

```python
_SLOT_LOAD_RE  = re.compile(r'^\s*(?:\w+\s*\*\s*)?(\w+)\s*=\s*(\w+)\s*->\s*(\w+)\s*;', re.M)
_SLOT_NULL_RE  = re.compile(r'^\s*(\w+)\s*->\s*(\w+)\s*=\s*NULL\s*;', re.M)
# For each (local, obj, fld) load, find a later `obj->fld = NULL;` followed within 2 lines by
# Py_DECREF(local).  If any PYTHON_REACHING_APIS call appears between the load and the NULL store,
# emit `stale_slot_decref` (confidence high).  Fix text: "use Py_CLEAR(obj->fld)".
```

**T2. `PYTHON_REACHING_APIS` table + `borrowed_ref_across_call` rule.** Implements RG-1. Add a data
table (`data/api_tables.json`) of calls that can execute arbitrary Python — `PyObject_Call*`,
`_PyObject_Call*`, `PyObject_Get/SetItem`, `PyObject_Get/SetAttr*`, `PyObject_Hash`,
`PyObject_Repr/Str/Format`, `PyObject_RichCompare*`, `PySequence_*`, `PyMapping_*`, `PyNumber_*`,
`PyIter_Next`, `PyDict_SetItem`, `PyList_Append`, `PyErr_Format` (`%R`/`%T`/`%S`), and **`Py_DECREF`
/ `Py_XDECREF` / `Py_CLEAR` / `Py_SETREF`** (a `__del__` is arbitrary Python — this is what makes
finding 1 detectable). Then flag: borrowed load → Python-reaching call → later use of the same local,
with no `Py_INCREF`/`Py_NewRef` in between.

**T3. `owner_freed_before_use` sub-rule of T2.** Implements RG-2, and is much narrower than full T2 so
it can ship first: if `Py_X?DECREF(V)` (or `Py_CLEAR(V)`) is followed *in the same basic block* by a
read of `V` or of any variable assigned from `V` (`A = B = expr` aliasing counts), emit FIX. On the
sample this fires exactly once — on finding 1.

**T4. Four precision gates, ~20 lines total, that would have removed 16 of the 19 FPs.**
- `_ASSIGN_CALL_RE`: require the target not to be preceded by `.`, `->`, or `*`. Add an
  `ownership_transfer` classification for `*var =` and `x->m =`. *(kills 5 FPs)*
- Model `Py_SETREF`/`Py_XSETREF`'s **second** argument as consumed:
  `re.compile(r'Py_X?SETREF\s*\(\s*(\w+)\s*,\s*(\w+)')` → add both groups to `decreffed`. *(2 FPs)*
- Treat a new-ref variable passed as an argument to **any** non-modeled call as escaped; downgrade to
  `confidence: "low"` or suppress. *(2 FPs)*
- `potential_leak_on_error`: suppress when the assignment's `line_offset` is greater than the offset of
  the last `goto <label>` in the function. *(2 FPs)*

**T5. Rewrite `_is_tp_init`/`_is_tp_new` to require slot registration.** Match the identifier against
`.tp_init =` / `.tp_new =` / a positional `PyTypeObject` initialiser, or an Argument Clinic
`__new__ as <name>` / `@classmethod ... __new__` marker — not a name suffix. *(kills 6 FPs)*
Then gate `new_missing_member_init` on *a fallible call occurring between the allocator and the member
stores* and accept `param->member = <anything>;` (not just `NULL`/`0`) as initialisation. Correct the
`detail` string: `object.__new__` does not call a custom `tp_new`.

**T6. `lazy_init_raw_store` finding type.** Implements RG-4:
`if (X->F == NULL) { X->F = <fallible_call>(...); }` where `<fallible_call>` is in
`PYTHON_REACHING_APIS` → CONSIDER, "use `Py_XSETREF` and wrap in `Py_BEGIN_CRITICAL_SECTION`; cf.
gh-153298". Cross-file: also feed these to `scan_ft_races.py`.

**T7. Delete the dead code** (`_ANY_API_CALL_RE`, `_RETURN_NULL_RE`, `INCREF_APIS`, `increffed`, the
`pass`-bodied block at :645-660) or wire it up. Its presence is what makes the briefing claim the
crown-jewel shape is covered when it is not.

**T8. Add to `data/cpython_non_bugs.md`:** *"Constructor helpers named `*_New` that are not `tp_new`
slots. `object.__new__(T)` allocates through `tp_alloc` (zeroing) and refuses when `tp_new` is
overridden — a C-API constructor that stores real values into every member immediately after
`PyObject_GC_New` is correct, not an uninitialised-member bug."*
