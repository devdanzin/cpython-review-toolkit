# Error Path Analysis Results — slice `obj-typeobject`, PASS 1

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scope:** `Objects/typeobject.c` only (13,068 lines)
**Mode:** INFORMED (briefing + FP taxonomy read first)
**Builds used:** `~/projects/python_build_matrix/builds/{debug,release}-gil-nojit/python` @ `a1d580430c8`.
`git diff a1d580430c8 4f3be1b5777 -- Objects/typeobject.c Objects/dictobject.c Include/internal/pycore_object.h`
is **empty** — every file cited below is byte-identical between the build and the target ref.

## Summary

- Functions analyzed (scanner): 362 (`scan_error_paths`) / 417 (`scan_pyerr_clear`)
- `PyErr_Clear` sites in the file: **11** — all 11 accounted for below
- `unconditional_pyerr_clear`: 5 candidates → **3 FIX, 1 CONSIDER, 1 ACCEPTABLE (taxonomy FP class)**
- `pyerr_clear_unfiltered_after_python_call`: 1 candidate → **1 CONSIDER** (same `(file,line)` as an
  `unconditional_pyerr_clear` candidate; deduped)
- `alloc_null_no_memerror`: 0 candidates; **hand-verified 7/7 raw allocation sites — clean negative, denominator 7**
- `return-null-without-exception` in the construction surface: **0 found** (see §4) — clean negative
- **New by reading (no scanner flagged them): 3** — one true miss of an existing rule (`:6183`) and two
  outside any current rule's shape (`:11090`, `:11227`)

Line numbers verified against the file: `grep -n PyErr_Clear Objects/typeobject.c` →
2405, 3023, 6149, 6158, 6183, 7490, 8619, 9748, 9786, 10931, 11032. Scanner was line-accurate 6/6.

---

## 1. The guarded twins in this file

Three of them, all in `Objects/typeobject.c`, all load-bearing for the findings below.

| twin | line | what it does right |
|---|---|---|
| `type_add_method` | 8614-8620 | calls the *same* `type_module()` and narrows: `if (!PyErr_ExceptionMatches(PyExc_AttributeError)) return -1; PyErr_Clear();` |
| `vectorcall_maybe` / `slot_tp_richcompare` / `slot_tp_iter` | 3149 / 11075 / 11101 | disambiguate the "NULL means absent *or* error" sentinel with `if (PyErr_Occurred())` before deciding |
| `type_new_set_names` | 12290 | the **gh-112453 fix**: `if (set_name == NULL) { if (PyErr_Occurred()) goto error; continue; }` — upstream precedent that this exact swallow is treated as a bug |

Plus, for the destructor family: `type_dealloc_common:6851-6855` brackets `remove_all_subclasses`
with `PyErr_GetRaisedException()` / `PyErr_SetRaisedException()`. That is the correct pattern and it
discharges `:9786`.

---

## 2. Findings

### [FIX] `type_repr` clears whatever a user `__eq__` raised (typeobject.c:2405, `type_repr`)

```c
    PyObject *mod = type_module(type);
    if (mod == NULL) {
        PyErr_Clear();
    }
```

**What.** `type_module()` (`:1607`) does `PyDict_GetItemRef(dict, &_Py_ID(__module__), &mod)` on the
type's `tp_dict`. When `tp_dict` is not unicode-only, the dict probe compares the *stored* key with
`PyObject_RichCompare(startkey, key, Py_EQ)` — arbitrary Python. The author's intent is to swallow
the `AttributeError: __module__` that `type_module` raises itself; the clear is unnarrowed, so it
also swallows anything the stored key's `__eq__` raised.

**Not the "sentinel-returning lookup" FP class.** The taxonomy's own refinement covers this exactly:
"an *unfiltered* clear after a call that runs arbitrary Python is NOT in the idiomatic class."
`type_module` is a lookup, but the lookup runs user `__eq__`.

**Guarded twin.** `type_add_method:8614-8620` — same callee, narrows to `AttributeError` first.
Two more callers propagate rather than clear: `type_get_module:1637` and
`_PyType_GetFullyQualifiedName:1669`. So 3 of the 5 `type_module()` call sites handle it correctly
and the 2 that clear (`:2405`, `:7490`) are the outliers.

**Python reachability — REPRODUCED**, both `debug-gil-nojit` and `release-gil-nojit`.
`scratchpad/t4_strsubclass.py` (no `RuntimeWarning`, `warnings.simplefilter("error", RuntimeWarning)`
is active):

```python
class S(str):
    def __eq__(self, other):
        if armed[0]: raise KeyboardInterrupt("EXC-FROM-USER-__eq__")
        return str.__eq__(self, other)
    __hash__ = str.__hash__

X = type('X', (), {S('__module__'): 'modname'})
armed[0] = True
repr(X)            # -> "<class 'X'>"        KeyboardInterrupt GONE
X.__module__       # -> KeyboardInterrupt    (control: type_get_module does not clear)
```

A plain `Evil` key with `__hash__ = lambda: hash('__module__')` works identically
(`scratchpad/t1_type_repr.py`) but costs a `RuntimeWarning: non-string key in the __dict__ of class X`
(`typeobject.c:4964`); the `str` subclass avoids the warning entirely because
`_PyDict_HasOnlyStringKeys` uses `PyUnicode_Check`, not `CheckExact`.

**Fix.**
```c
    PyObject *mod = type_module(type);
    if (mod == NULL) {
        if (!PyErr_ExceptionMatches(PyExc_AttributeError)) {
            return NULL;
        }
        PyErr_Clear();
    }
```

---

### [FIX] `object_repr` — the same clear, the same twin (typeobject.c:7490, `object_repr`)

```c
    mod = type_module(type);
    if (mod == NULL)
        PyErr_Clear();
```

Structural duplicate of `:2405`; same guarded twin at `:8619`. **REPRODUCED** on both builds in the
same script: `repr(X())` returns `'<X object at 0x...>'` with the `KeyboardInterrupt` discarded.
Fix is identical except the early return is `Py_XDECREF(mod); return NULL;`-shaped.

Blame note: `:2405` arrived with `c432df6d56f` (gh-111696, PEP 737, 2024) — *after* the twin at
`:8619` was written with the narrowing (`15c9f2491d2`, gh-115231). The house rule was already
established when the outlier was added.

---

### [FIX] `find_name_in_mro` clears a user `__hash__` and returns "not found" (typeobject.c:6149)

```c
    Py_hash_t hash = _PyObject_HashDictKey(name);
    if (hash == -1) {
        PyErr_Clear();
        return -1;
    }
```

`_PyObject_HashDictKey` (`pycore_object.h:840`) fast-paths only `PyUnicode_CheckExact`; anything else
goes to `PyObject_Hash` → user `__hash__`.

**Why this is not just the documented convention.** The function's header comment says
"It never set an exception. Returns -1 if there was an error, 0 if the name was not found" — so the
*clear* is intentional. But the caller cannot act on it: `_PyType_LookupRefAndVersion:6368-6371`
turns `res < 0` into `*out = PyStackRef_NULL; return 0;`, i.e. **error becomes "absent"**. There is
no `PyErr_Occurred()` disambiguation anywhere in the chain, unlike every other sentinel consumer in
this file (§1, row 2). The observable result is a *wrong answer*, not merely a swallowed exception.

**REPRODUCED**, both builds (`scratchpad/t2_find_name_in_mro.py`):

```python
class S(str):
    def __hash__(self): raise KeyboardInterrupt("EXC-FROM-USER-__hash__")

getattr(int, S('bit_length'))
# -> AttributeError: type object 'int' has no attribute 'bit_length'
```

`int.bit_length` exists. The `KeyboardInterrupt` is gone and the interpreter asserts a falsehood.

**Guarded twin.** `vectorcall_maybe:3145-3151` — same "lookup returned nothing" situation, resolved
with `if (!PyErr_Occurred()) Py_RETURN_NOTIMPLEMENTED; return NULL;`. The twin is possible only
because `lookup_maybe_method` leaves the exception *set*; `find_name_in_mro` destroys that
information before its caller can use it.

**Fix (minimal, contract-preserving).** Make `find_name_in_mro` stop clearing and let
`_PyType_LookupRefAndVersion` do it, so the one caller that wants "never raises" keeps it while the
error/absent distinction survives long enough to be used:
```c
    if (hash == -1) {
        return -1;                 /* leave the exception set */
    }
```
and in `_PyType_LookupRefAndVersion`, `if (res < 0) { PyErr_Clear(); ... }` — or, better, propagate.
Note `_PyType_Lookup`'s "never sets an exception" contract is depended on by the
`assert(!PyErr_Occurred())` at `:6351`, `:11938` and `:12134`, so this is a POLICY decision upstream
has to take; see §5.

---

### [FIX] `find_name_in_mro` clears a user `__eq__` from the MRO dict probe — **SCANNER MISS** (typeobject.c:6183)

```c
        Py_ssize_t ix = _Py_dict_lookup_threadsafe_stackref(
            (PyDictObject *)dict, name, hash, out);
        if (ix == DKIX_ERROR) {
            PyErr_Clear();
            res = -1;
            goto done;
        }
```

Not flagged by `scan_error_paths` nor `scan_pyerr_clear`. Root cause: the failure-branch recognizer
does not treat `== DKIX_ERROR` as an error test (it looks for `NULL` / `0` / `-1`). See §6.

The comment eleven lines above **names the hazard**: "type->tp_mro can be replaced during dict lookup,
e.g. when comparing to non-string keys." So the file knows user code runs here, and the clear is still
unnarrowed.

**REPRODUCED**, both builds, and this one has a Python-visible consequence beyond the swallow —
the swallow reaches `update_one_slot:11942`, whose `if (res <= 0) { ... continue; }` treats the error
as "name not in MRO", so `slot_value` falls through to `NULL` and the slot is **silently cleared**
(`scratchpad/t3_update_one_slot.py`, `t4_strsubclass.py`):

```python
class S2(str):
    def __eq__(self, other):
        if armed2[0]: raise KeyboardInterrupt("EXC-FROM-USER-__eq__")
        return str.__eq__(self, other)
    __hash__ = str.__hash__

Base = type('Base', (), {S2('__init__'): 'decoy'})   # collides with hash('__init__')
class C(Base):
    def __init__(self): pass

armed2[0] = True
del C.__init__          # -> update_slot -> update_one_slot -> find_name_in_mro:6183 -> swallowed
armed2[0] = False
C(1, 2, 3)              # ACCEPTED.  tp_init is NULL; object.__init__ was never installed.
```

`C(1,2,3)` must raise `TypeError: object.__init__() takes exactly one argument`. It returns an
instance instead. The C-level slot table and the Python-level MRO now disagree, permanently, with no
error anywhere. `fixup_slot_dispatchers:12131` discards `update_one_slot`'s return by design, so the
same desync is reachable at class-creation time too (the `RuntimeWarning` at `:4964` is emitted
*after* `fixup_slot_dispatchers`, and a `str` subclass key does not trigger it at all).

**Guarded twin.** Same as `:6149` — `vectorcall_maybe:3149`. Fix is the same: stop discarding the
error/absent distinction.

---

### [CONSIDER] `find_name_in_mro` clears whatever `PyType_Ready` raised (typeobject.c:6158)

Flagged twice (`unconditional_pyerr_clear` **and** `pyerr_clear_unfiltered_after_python_call`);
deduped to one site by `(file, line)`.

```c
        if (!is_readying(type)) {
            if (PyType_Ready(type) < 0) {
                PyErr_Clear();
                return -1;
            }
```

The double flag raised the prior, but the reachability is weaker than the other two. This branch is
only entered when `lookup_tp_mro(type) == NULL`, i.e. a static type that has not been readied — never
true for anything reachable from Python. `PyType_Ready` *can* run arbitrary Python
(`type_ready_mro` → `mro_internal` → `mro_invoke` calls a custom metatype's `mro()`), and it can fail
with `MemoryError` from any of its twelve sub-steps; both are then discarded and the caller reports
`AttributeError`. **I could not build a pure-Python repro** — it needs a C extension that calls
`PyType_Ready` on a type with a heap metaclass. Classified CONSIDER on that basis, not because the
shape is wrong. The fix is the same one-liner as `:6149`.

---

### [ACCEPTABLE] `get_subclasses_key` (typeobject.c:9748) — taxonomy FP class

The taxonomy already names **this exact line** as an exemplar of "Early-return guard clause":

```c
    PyObject *key = PyLong_FromVoidPtr((void *) type);
    if (key != NULL) {
        return key;
    }
    PyErr_Clear();
    /* This basically means we're out of memory.
       We fall back to manually traversing the values. */
```

The clear has no enclosing conditional but is fully dominated by the negation of `key != NULL`, and
the only thing `PyLong_FromVoidPtr` can raise is `MemoryError`, which the function deliberately
converts into a slower O(n) fallback. Dismissed, no re-litigation.

---

## 3. New siblings found by reading (no scanner rule covers these shapes)

### [FIX] `has_dunder_getitem` drops `lookup_maybe_method`'s error; `slot_tp_iter` then destroys the exception (typeobject.c:11090 / :11108)

```c
static int
has_dunder_getitem(PyObject *self)
{
    ...
    lookup_maybe_method(self, &_Py_ID(__getitem__), &c_ref.ref);   /* :11090 — return DISCARDED */
    int has_dunder_getitem = !PyStackRef_IsNull(c_ref.ref);
    ...
}
```

`lookup_method_ex:3018-3026` clears **only** `AttributeError` (`raise_attribute_error == 0` path).
Any other exception raised by the `__getitem__` descriptor's `tp_descr_get` is left **pending** and
signalled by the `-1` return. `has_dunder_getitem` discards that return and reports `0`.
`slot_tp_iter:11108` then does:

```c
    else if (attr_is_none || !has_dunder_getitem(self)) {
        PyErr_Format(PyExc_TypeError, "'%.200s' object is not iterable", ...);
```

`PyErr_Format` → `_PyErr_SetObject` chains against the *handled* exception stack, not the pending
one, so the live exception is not chained — it is **annihilated**.

**REPRODUCED**, both builds (`scratchpad/t5_has_dunder_getitem.py`):

```python
class RaisingGet:
    def __init__(self, exc): self.exc = exc
    def __get__(self, obj, objtype=None): raise self.exc

class C:
    __iter__    = RaisingGet(AttributeError("no __iter__ here"))
    __getitem__ = RaisingGet(KeyboardInterrupt("EXC-FROM-__getitem__.__get__"))

iter(C())
# -> TypeError: 'C' object is not iterable
#    __context__: None            <-- KeyboardInterrupt is entirely gone
C().__getitem__                   # control: KeyboardInterrupt propagates normally
```

**Guarded twin — three of them, all in this file.** `lookup_maybe_method` has six call sites; five
handle the sentinel:
`vectorcall_maybe:3145` (`if (!PyErr_Occurred())`), `maybe_call_special_no_args:3171` and
`maybe_call_special_one_arg:3192` (return `NULL` with the exception intact for the caller to test),
`slot_tp_finalize:11227` (`if (unbound >= 0)`). Only `:11090` drops it. The exact disambiguation is
also the shipped fix for **gh-112453** at `type_new_set_names:12290`.

**Prior art:** none for this site (`gh api search/issues` on `has_dunder_getitem`,
`slot_tp_iter TypeError hides exception`, `object is not iterable descriptor __get__ exception` —
all empty). The closest relative is the **open gh-131151**, "richcompare suppresses all errors raised
from descriptors of comparison special methods", which is the same family one hop away
(`lookup_method_ex:3023`'s `AttributeError` narrowing). This is a distinct, worse instance: `:11090`
loses *non*-`AttributeError` exceptions.

**Fix.**
```c
static int
has_dunder_getitem(PyObject *self)
{
    ...
    int res = lookup_maybe_method(self, &_Py_ID(__getitem__), &c_ref.ref);
    int has = !PyStackRef_IsNull(c_ref.ref);
    _PyThreadState_PopCStackRef(tstate, &c_ref);
    if (res < 0 && PyErr_Occurred()) {
        return -1;                       /* propagate */
    }
    return has;
}
```
with `slot_tp_iter` testing for `-1` before formatting its `TypeError`.

---

### [CONSIDER] `slot_tp_finalize` restores over an exception from `__del__.__get__` with no unraisable report (typeobject.c:11227 + :11243)

```c
    PyObject *exc = _PyErr_GetRaisedException(tstate);
    int unbound = lookup_maybe_method(self, &_Py_ID(__del__), &cref.ref);
    if (unbound >= 0) {
        ...
        if (res == NULL) {
            PyErr_FormatUnraisable("Exception ignored while calling deallocator %R", del);
        }
    }
    ...
    _PyErr_SetRaisedException(tstate, exc);      /* :11243 — overwrites a live exception */
```

The `unbound >= 0` test is correct as a *control-flow* guard, but on the `-1` branch a non-
`AttributeError` exception raised by the `__del__` descriptor's `__get__` is still pending and is then
overwritten by the restore, unreported. **The guarded twin is five lines above**: an exception from
`__del__`'s *body* is reported via `PyErr_FormatUnraisable`. The asymmetry is the finding.

**REPRODUCED**, both builds (`scratchpad/t6_finalize.py`, with `sys.unraisablehook` installed):
`__del__ = RaisingGet()` → **zero** unraisable reports; `def __del__(self): raise ...` → one report.

CONSIDER rather than FIX: this is the destructor family, where dropping is the house norm — but the
norm is *report-then-drop*, and this path silently drops. One-line fix: add a
`PyErr_FormatUnraisable("Exception ignored while looking up deallocator of %R", self);` on the
`unbound < 0 && PyErr_Occurred()` path.

---

## 4. Clean negatives (denominators stated)

**Allocators — `alloc_null_no_memerror`, 0 findings over a denominator of 7.** The scanner reported
zero for the slice; I read every raw allocation in the file to check the zero was earned:

| line | allocation | discharges |
|---|---|---|
| 2528 | `_PyObject_MallocWithType` | `return PyErr_NoMemory();` |
| 3371 | `PyMem_New(Py_ssize_t, …)` | `PyErr_NoMemory(); return -1;` |
| 3488 | `PyMem_New(PyObject *, n+1)` | `PyErr_NoMemory(); return NULL;` |
| 3838 | `PyMem_Malloc` (`slot_update_new_chunk`) | `PyErr_NoMemory(); return NULL;` |
| 4556 | `PyMem_Malloc` (`tp_doc`) | `PyErr_NoMemory(); return -1;` |
| 5423 | `PyMem_Malloc` (spec `tp_doc`) | `PyErr_NoMemory(); goto finally;` |
| 5501 | `PyMem_Malloc` (`_ht_tpname`) | `PyErr_NoMemory(); goto finally;` |

7/7, including the two `PyMem_New` sites that the taxonomy warns are *not* self-raising. Earned zero.

**`return-null-without-exception` in the construction surface — 0 findings.** Read
`type_new` (4191-5135), `type_from_slots_or_spec` (5248-5801), `type_ready`/`PyType_Ready`
(9017-9691) and `update_one_slot` (11907-12067):

- `type_from_slots_or_spec:5790` — `finally: if (PyErr_Occurred()) { Py_CLEAR(res); }`. This is the
  *other* direction of the desync handled correctly: a success return with a pending exception is
  converted into a proper `NULL`. Exemplary; cite it as the guarded twin if this shape shows up in
  pass 2.
- `type_ready` is a twelve-step `goto error` ladder; every step is an `int`-returning helper that
  raises before returning `-1`.
- `type_new_impl:5103` asserts `PyErr_Occurred()` on the `type_new_get_bases` error return —
  the invariant is stated, not assumed.
- `update_one_slot`'s only `-1` is `queue_slot_update` → `slot_update_new_chunk` → `PyErr_NoMemory()`
  (free-threaded build only). `fixup_slot_dispatchers:12131` discards the return, but passes
  `queued_updates == NULL`, so `-1` is unreachable there. Deliberate, not a defect.
- `type_module:1607` relies on `PyDict_GetItemRef` setting `*result = NULL` on its `-1` path;
  verified at `Objects/dictobject.c:2601-2617` — it does, on both error branches. No uninitialized
  `mod` escapes.

**All 11 `PyErr_Clear()` sites accounted for.** The five not covered above:

| line | function | verdict |
|---|---|---|
| 3023 | `lookup_method_ex` | ACCEPTABLE — narrowed with `PyErr_ExceptionMatches(PyExc_AttributeError)`. (Subject of the open gh-131151; confirming, not re-litigating.) |
| 8619 | `type_add_method` | ACCEPTABLE — **the guarded twin**, narrowed to `AttributeError`. |
| 9786 | `remove_subclass` | ACCEPTABLE — teardown path; `key` is always a `PyLong` from `PyLong_FromVoidPtr`, so `PyDict_DelItem` can only raise `KeyError` (hashing and comparing `int`s is infallible). The caller `type_dealloc_common:6851` brackets it with `PyErr_GetRaisedException`/`SetRaisedException`, and `remove_all_subclasses` asserts `!PyErr_Occurred()` on both ends. Narrowing to `KeyError` would still be tidier. |
| 10931 | `slot_tp_hash` | ACCEPTABLE — "statically-known type slot" FP class: `res` is `PyLong_Check`-ed, so `PyLong_AsSsize_t` can only raise `OverflowError` and the fallback calls the fixed C function `PyLong_Type.tp_hash`. No user code. |
| 11032 | `slot_tp_getattr_hook` | ACCEPTABLE — narrowed with `PyErr_ExceptionMatches(PyExc_AttributeError)` before falling back to `__getattr__`. |

---

## 5. POLICY

**`find_name_in_mro` / `_PyType_Lookup`'s "never sets an exception" contract is load-bearing and
undocumented at the API boundary.** `find_name_in_mro:6141-6144` states it in a comment; nothing in
`Include/internal/pycore_typeobject.h` does. Three `assert(!PyErr_Occurred())` sites depend on it
(`:6351`, `:11938`, `:12134`). The contract is defensible for speed, but it currently costs:

1. a wrong `AttributeError` for an attribute that exists (§`:6149`),
2. a silently mis-wired slot table (§`:6183`),

and it is the only sentinel consumer family in this file that *cannot* disambiguate, because the
callee destroys the evidence before returning. The minimal POLICY-preserving change is to move the
`PyErr_Clear()` from `find_name_in_mro` up into `_PyType_LookupRefAndVersion` — the contract is
unchanged for every existing caller, and the two internal callers gain the ability to distinguish
"absent" from "failed". Upstream precedent that these swallows are treated as bugs: **gh-112453**
(fixed, `type_new_set_names`) and the still-open **gh-131151**.

---

## 6. Toolkit assessment

### Precision

| rule | candidates | FIX | CONSIDER | ACCEPTABLE | precision (FIX) | precision (FIX+CONSIDER) |
|---|---|---|---|---|---|---|
| `unconditional_pyerr_clear` | 5 | 3 | 1 | 1 | 60% | 80% |
| `pyerr_clear_unfiltered_after_python_call` | 1 | 0 | 1 | 0 | — | 100% |
| `alloc_null_no_memerror` | 0 (denom 7) | — | — | — | earned zero | earned zero |
| `missing_null_check` / `unchecked_return` / `unchecked_parse` | 0 | — | — | — | earned zero | earned zero |

The single dismissal (`:9748`) is a taxonomy FP class the taxonomy *already names by line number* —
it should be suppressed by the scanner, not by the agent. See T3 below.

The `pyerr_clear_unfiltered_after_python_call` rule earned its keep on a different axis: it was the
rule that raised the prior on `:6158` by flagging it twice. That double-flag mechanism worked, and it
should be preserved — but note the two rules must be deduped by `(file, line)`, not by normalized
detail string (the taxonomy's `xibufferview_dealloc` warning applies here: `:6149` and `:6158` have
byte-identical detail strings and survived only because `scan_error_paths` keys on the line).

### Recall gaps (the more valuable output)

**T1 — `DKIX_ERROR` is not recognized as a failure test (one true positive missed).**
`find_name_in_mro:6183` is the *most* reachable of the three clears in that function and neither
scanner flagged it, because the branch is `if (ix == DKIX_ERROR)` and the failure-branch recognizer
only accepts comparisons against `NULL` / `0` / `-1`. Concrete proposal: extend the recognizer to
`== <ALL_CAPS_IDENT>` where the identifier matches `/_(ERROR|ERR|FAIL(ED)?)$/` or is a known dict
sentinel (`DKIX_ERROR`, `DKIX_EMPTY`). Cheap, and it also picks up `_Py_dict_lookup*` consumers
elsewhere in `Objects/`.

**T2 — a new rule: `discarded_fallible_result`.** The two findings the scanners could not see
(`has_dunder_getitem:11090`, and, in the same family, `fixup_slot_dispatchers:12131`) have one
mechanical shape:

> a call to an `int`-returning file-local helper, appearing as a bare *expression statement* (no
> assignment, no `(void)` cast, not inside a condition), where **another call site in the same file
> tests the same callee's return**.

That in-file-disagreement predicate is exactly the "guarded twin" heuristic made mechanical, and it
keeps the rule quiet: `lookup_maybe_method` has 6 call sites, 5 of which test the result, 1 of which
does not — and the 1 is the reproduced bug. `fixup_slot_dispatchers:12131` would also fire and is a
justified dismissal (`queued_updates == NULL` makes `-1` unreachable), so expect ~50% precision on
this file with n=2. Worth a slice to calibrate. Note CPython uses `(void)` explicitly when a discard
is deliberate (`(void)PyCell_Set(...)` at `:4768`, `:4794`), which gives the rule a free
suppression channel.

**T3 — suppress the "early-return guard clause" class in the scanner, not the agent.**
`:9748` is named verbatim in the taxonomy and still costs a candidate slot every run. The rule
already needs dominance analysis for `if (x != NULL) return x;`; the taxonomy's list of terminators
(`Py_RETURN_*`, `Py_UNREACHABLE`, module-local `FAIL`) should be applied here too.

**T4 — the `type_module()` family is a ready-made sibling-hunt template.** Generalize: *a file-local
helper that raises `AttributeError` as its "absent" signal, where some callers narrow with
`PyErr_ExceptionMatches(PyExc_AttributeError)` and others clear bare.* Here it was 3 correct
(`:1637`, `:1669`, `:8619`) vs 2 wrong (`:2405`, `:7490`) — a 60/40 split that a majority-vote
heuristic would have surfaced automatically. This is worth a scanner rule of its own: for every
`PyErr_Clear()` reacting to callee `F`, check whether *any other* call site of `F` in the same file
narrows first; if so, flag with high confidence.

### Reproduction notes for the record

- Ambient `python` is RustPython — every run above named an explicit interpreter.
- Heredocs were avoided; all five repros are `.py` files under
  `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/`:
  `t1_type_repr.py`, `t2_find_name_in_mro.py`, `t3_update_one_slot.py`, `t4_strsubclass.py`,
  `t5_has_dunder_getitem.py`, `t6_finalize.py`.
- Every finding reproduces identically on `debug-gil-nojit` and `release-gil-nojit`; none is
  debug-only or assert-dependent.
