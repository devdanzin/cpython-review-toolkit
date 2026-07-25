# pyerr-clear-auditor — slice `obj-typeobject`, **PASS 2**

**Target:** `/home/danzin/projects/cpython/Objects/typeobject.c` @ `4f3be1b5777` (3.16.0a0), 13,068 lines
**Builds used:** `debug-gil-nojit`, `release-gil-nojit`, `debug-ft-nojit` @ `a1d580430c8`.
`git diff --stat 4f3be1b5777 a1d580430c8 -- Objects/typeobject.c Objects/dictobject.c Python/errors.c Objects/object.c`
is **empty** — all four files are byte-identical between target and build refs, so every citation and every
repro below applies unmodified to the target ref.

**Relationship to pass 1.** Pass 1's report
(`/home/danzin/projects/cpython-review-toolkit/reports/obj-typeobject/agents/pyerr-clear-auditor.md`)
enumerated and settled all 11 `PyErr_Clear` sites, swept the destructor family, and found
`slot_tp_finalize:11243`. **None of that is re-litigated.** This report re-verifies the census (§0), fully
triages the one scanner finding (§1), and then spends its budget on what pass 1 could not see: a **net-new
reproduced FIX in R21** (§2), a **reachability upgrade + consumer census** for the recorded
`find_name_in_mro` clear across the pass-2 regions (§3), the destructor functions pass 1's enumeration did
not name (§4), and the recall gaps (§7).

---

## Summary

- **Clears examined: 11** (raw `grep -c PyErr_Clear` = 11, `_PyErr_Clear` = 0 — the census is complete and
  the scanner now attributes **11 of 11**, see §7). **Destructor-family functions: 15.**
- **Non-`PyErr_Clear` exception-loss mechanisms examined: 12** (5 `PyErr_FormatUnraisable`, 3
  `Get/SetRaisedException` pairs, 4 unnarrowed `PyErr_Format`-over-a-failure sites).
- **FIX 1 · CONSIDER 1 · POLICY 1 · ACCEPTABLE 10** (this report's own verdicts; sites already carrying a
  CPY- record are confirmed in one line and counted under their existing record).

| sub-class | sites | verdict |
|---|---|---|
| **(a)** unguarded clear in the destructor family | **0 of 11** clears; 15 destructor fns read | genuine zero — §4 |
| **(b)** clear on a provable success path (gh-146102 class) | **0 of 11** | genuine zero — §5 |
| **(c)** unnarrowed clear of what user code raised | **3 of 11** (`:6149`, `:6158`, `:6183`) | all recorded (CPY-0074/0077); **reach widened** — §3 |
| **(d)** *unnarrowed exception **replacement*** (pass-2 addition to the taxonomy) | **1 net new** | **FIX**, reproduced — §2 |

---

## 0. Census — re-verified, unchanged from pass 1

```
grep -c PyErr_Clear  Objects/typeobject.c   -> 11
grep -c _PyErr_Clear Objects/typeobject.c   -> 0
lines: 2405 3023 6149 6158 6183 7490 8619 9748 9786 10931 11032
```

Identical to pass 1's list. Verdicts carried forward verbatim; the two pass 1 flagged as
**scanner-invisible** (`:10931`, `:11032`) are now visible (§7) and both re-read as ACCEPTABLE:

- `:10931` `slot_tp_hash` — clears only the `OverflowError` that `PyLong_AsSsize_t` can raise, then hashes
  through the **statically known** `PyLong_Type.tp_hash`. No user code between. ACCEPTABLE.
- `:11032` `slot_tp_getattr_hook` — **this is the `__getattr__`-fallback pattern the brief asks about, and
  it is correctly narrowed**: `if (res == NULL && PyErr_ExceptionMatches(PyExc_AttributeError))` guards the
  clear before falling back to `call_attribute(self, getattr, name)`. ACCEPTABLE, and it is the in-file
  **guarded twin** for that whole pattern. The sibling arm at `:11024` uses the
  `res == NULL && !PyErr_Occurred()` form (the suppress-flag protocol), which is equally narrow.
  There is **no unnarrowed `__getattr__` fallback in this file.**

`:3023` (`lookup_method_ex`) and `:8619` (`type_add_method`) are the other two narrowed clears.
Narrowing rate: **3 of 11 explicit `PyErr_ExceptionMatches`**, 2 more narrowed by construction,
3 covered by a caller-side `assert(!PyErr_Occurred())`, 2 clearing their own error under a bracket.

---

## 1. [POLICY] The one scanner finding — `find_name_in_mro:6158` (`failing_call: PyType_Ready`)

**Confirmed as pass 1 has it; not re-litigated.** Triaged fully for the record:

- The clear is *inside the declared contract*. The function header at `:6140-6143` reads
  `It never set an exception.` and the return convention is tri-state (`-1` error, `0` not found, `1` found).
- Both callers discharge the caller-owned-exception obligation explicitly:
  `_PyType_LookupStackRefAndVersion:6350-6351` and `update_one_slot:11937-11938` each carry
  `/* We may end up clearing live exceptions below, so make sure it's ours. */` + `assert(!PyErr_Occurred());`.
- The `failing_call` the scanner named — `PyType_Ready` — **cannot run user Python on this path**. It is
  reached only when `lookup_tp_mro(type) == NULL && !is_readying(type)`, i.e. a type that has never been
  readied. Its realistic failure is `MemoryError`, which is flattened into "not found". That is a
  documented-contract loss, not a user-exception swallow.
- **Guarded twin (required by the brief):** `Objects/typeobject.c:11032` `slot_tp_getattr_hook` — the same
  file, the same "a lookup failed, fall back" shape, narrowed with `PyErr_ExceptionMatches(PyExc_AttributeError)`
  before clearing. The cross-file twin the scanner cites, `Objects/genericaliasobject.c` `set_orig_class`, is
  the same shape.

**Verdict: POLICY.** No rule change proposed — `find_name_in_mro` is already inside the agent spec's
documented-contract carve-out, and the scanner's `medium` confidence is calibrated correctly.

*(The sibling clears at `:6149` and `:6183` are the ones with teeth. They are recorded — CPY-0077 by
mechanism, CPY-0074 by line `:6183` — and §3 does not re-litigate them; it reports what pass 2 adds.)*

---

## 2. **[FIX] `type_update_dict:6714-6719` converts *every* dict-insert failure into `AttributeError`, unnarrowed — a `MemoryError` from `T.x = 1` is destroyed**

**NET NEW.** Region R21, inside `type_setattro` — the #3 function on the pass-2 history watchlist
(7 crash fixes in 3 years, **2 in the last 12 months**). Invisible to all three current scanner rules
because there is no `PyErr_Clear` token anywhere near it.

**What.** `/home/danzin/projects/cpython/Objects/typeobject.c:6714`:

```c
    if (_PyDict_SetItem_LockHeld(dict, name, value) < 0) {
        PyErr_Format(PyExc_AttributeError,
                     "type object '%.50s' has no attribute '%U'",
                     ((PyTypeObject*)type)->tp_name, name);
        _PyObject_SetAttributeErrorContext((PyObject *)type, name);
        return -1;
    }
```

The `AttributeError` conversion is correct for exactly one failure mode — `value == NULL` (i.e. `del T.x`)
raising `KeyError` for an absent key. It is applied to **all** of them. `_PyDict_SetItem_LockHeld` also
fails with `MemoryError` when the type's `tp_dict` must grow, and that `MemoryError` is destroyed:
`_PyErr_SetObject` (`/home/danzin/projects/cpython/Python/errors.c`) opens the normalization path with an
explicit `_PyErr_Clear(tstate)` (`/* Issue #23571: functions must not be called with an exception set */`),
and its implicit chaining reads `_PyErr_GetTopmostException(tstate)->exc_value` — the *handled* exception,
never the *pending* one. So `__context__` is `None` and nothing survives.

**Guarded twin (the fix, in the immediately adjacent code):**
`/home/danzin/projects/cpython/Objects/object.c:2104`, `_PyObject_GenericSetAttrWithDict` — the identical
two-call conversion, gated:

```c
  error_check:
    if (res < 0 && PyErr_ExceptionMatches(PyExc_KeyError)) {
        PyErr_Format(PyExc_AttributeError,
                        "'%.100s' object has no attribute '%U'",
                        tp->tp_name, name);
        _PyObject_SetAttributeErrorContext(obj, name);
    }
```

**Sibling hunt — 1 of 11.** All eleven `_PyObject_SetAttributeErrorContext` call sites in the tree
(`Objects/object.c` ×9, `Objects/dictobject.c:7502`, `Objects/typeobject.c:6718`) were read.
Nine fire on a branch where **no exception is pending** (the attribute is genuinely absent);
`object.c:2104` fires after a fallible call **and narrows**; `typeobject.c:6718` fires after a fallible
call and **does not**. It is the only unguarded instance in the tree.

**Reproduced** — same script, same injection index, three builds
(`<scratchpad>/pass2_pyerr/probe6.py`; single-allocation OOM via `_testcapi.set_nomemory(i, i+1)` around a
plain `setattr` loop):

| build | `T.x = 1` (typeobject.c:6714, **unnarrowed**) | `obj.x = 1` (object.c:2104, narrowed) |
|---|---|---|
| `debug-gil-nojit` | `AttributeError: type object 'T' has no attribute 'a15'` `__context__=None` | `MemoryError` |
| `release-gil-nojit` | `AttributeError: type object 'T' has no attribute 'a15'` `__context__=None` | `MemoryError` |
| `debug-ft-nojit` | `AttributeError: type object 'T' has no attribute 'a15'` `__context__=None` | `MemoryError` |

A broader sweep (`probe5.py`, indices 0–259) isolates it to injection indices 1–4; index 0 fails an earlier
allocation and yields a clean `MemoryError`, which is the control.

**Impact.** Under memory pressure, `T.x = 1` — the most ordinary type mutation in Python — raises
`AttributeError: type object 'T' has no attribute 'x'`. The message is not merely misleading, it is
*inverted*: it says the attribute is missing at the moment you are creating it. Any library that wraps
`setattr` in `try: ... except AttributeError:` (the standard idiom for "this object rejects attributes")
now silently swallows an `OOM`. The instance path, one file over, gets this right.

**Fix** (two words, matching `object.c:2104` verbatim):

```c
    if (_PyDict_SetItem_LockHeld(dict, name, value) < 0) {
        if (PyErr_ExceptionMatches(PyExc_KeyError)) {
            PyErr_Format(PyExc_AttributeError,
                         "type object '%.50s' has no attribute '%U'",
                         ((PyTypeObject*)type)->tp_name, name);
            _PyObject_SetAttributeErrorContext((PyObject *)type, name);
        }
        return -1;
    }
```

**Reachability caveat, stated plainly:** the only route is allocation failure. `name` is an exact interned
unicode with a cached hash (`type_setattro:6768-6785`), so no user `__hash__`/`__eq__` can fail the insert
— and the collision route that *would* run user code is intercepted 11 lines earlier by
`_PyDict_GetItemRef_Unicode_LockHeld:6703`, which propagates correctly (§3, twin #2). Prior art: none —
`gh api -X GET search/issues` on `repo:python/cpython type_update_dict AttributeError MemoryError`
returns 0.

---

## 3. [CONSIDER] The recorded `find_name_in_mro:6183` clear — pass 2 widens **reachability** and **impact**, and names two in-file guarded twins

**Root cause confirmed, not re-litigated:** CPY-0074 (line `:6183` + the `update_one_slot` consumer) and
CPY-0077 (the user-`__eq__` mechanism). What follows is what pass 2 adds. Both parts change the severity
picture, so this section recommends the two catalog records be **widened**, not that a new one be opened.

### 3a. Reachability upgrade — the trigger needs **no** warning-triggering construct

The obvious way to get a non-string key into a type's `tp_dict` is `type('C', (), {obj: 1})`, and CPython
warns about it: `Objects/typeobject.c:4960-4968` emits
`RuntimeWarning: non-string key in the __dict__ of class C`, added by `f7c05d7ad30` (2024-01-29,
gh-55664 / GH-105338). That warning caps the severity — a reviewer can reasonably say "you were told".

**It does not fire for a `str` subclass.** `_PyDict_HasOnlyStringKeys`
(`/home/danzin/projects/cpython/Objects/dictobject.c:1838`) tests `PyUnicode_Check(key)`, which is
subclass-permissive, while `insertdict`'s unicode-keys fast layout requires `PyUnicode_CheckExact`.
So a `str` subclass key:

- passes the "only string keys" test → **no `RuntimeWarning`**;
- still forces `DICT_KEYS_GENERAL` → the MRO lookup compares with the user's `__eq__`;
- → `_Py_dict_lookup_threadsafe_stackref` returns `DKIX_ERROR` → `PyErr_Clear()` at `:6183`.

Verified on all three builds (`<scratchpad>/pass2_pyerr/probe3.py`):
`warnings at class creation: NONE` · `every key passes PyUnicode_Check: True`.

### 3b. Impact upgrade — the cleared error becomes **silently wrong data**, not just a wrong `AttributeError`

Pass 1's recorded outcomes are "wrong `AttributeError`" (CPY-0077) and "silently cleared `tp_init`"
(CPY-0074). The pass-2 regions contain consumers whose failure mode is strictly worse: they treat
"lookup failed" as "the dunder is absent" and **succeed with the wrong answer**. Differential, same class,
armed vs. disarmed bomb (`probe2.py`, `probe3.py`, `probe4.py`; identical on debug-gil / release-gil / debug-ft):

| region | consumer | control | with a colliding key whose `__eq__` raises `KeyboardInterrupt` |
|---|---|---|---|
| R26 | `__reduce_ex__(2)` / `__getnewargs_ex__` | `(__newobj__, (C, 'REAL'), …)` | **`(__newobj__, (C,), …)` — succeeds, newargs dropped** |
| R26 | `__reduce_ex__(2)` / `__getnewargs__` | `(__newobj__, (C, 'REAL'), …)` | **`(__newobj__, (C,), …)` — succeeds, newargs dropped** |
| R26 | `__reduce_ex__(2)` / `__reduce__` | `(dict, ((1, 2),))` | **default reduce — the class's own `__reduce__` is ignored** |
| R26 | `copy.copy` / `__copy__` | `'REAL-COPY'` | **a default shallow copy — `__copy__` ignored** |
| R26 | `__reduce_ex__(2)` / `__getstate__` | `(…, {'REAL': 1}, …)` | `AttributeError: 'C' object has no attribute '__getstate__'` |
| pass-1 | `C()` / `__init__` | `True` | `AttributeError(__init__)` |

End-to-end (`probe4.py`, `__getstate__` returning `None` so the value travels only via newargs):

```
dumps(Clean('SECRET'))    raised: None      clean    round-trip tag: 'SECRET'
dumps(Poisoned('SECRET')) raised: None      poisoned round-trip tag: 'DEFAULT-LOST'
VERDICT: SILENT DATA LOSS - KeyboardInterrupt was raised and discarded
```

`pickle.dumps` returns a **structurally valid pickle that round-trips to the wrong value**, with the
`KeyboardInterrupt` gone. That is the agent spec's headline harm ("a wrong-but-valid object") landing on a
serialization path, and it is a materially different outcome class from "a wrong `AttributeError`".

The chain is `_PyObject_LookupSpecial` → `_PyType_LookupRef` → `find_name_in_mro`. The contract is stated
out loud at `/home/danzin/projects/cpython/Objects/typeobject.c:2940-2941`:
*"`_PyObject_LookupSpecial()` returns NULL without raising an exception when the `_PyType_LookupRef()` call
fails"* — so the consumers' defensive `} else if (PyErr_Occurred()) { return -1; }` checks
(`_PyObject_GetNewArguments:8146` and `:8168`) are **dead for exactly this case** and cannot help.

### 3c. Two in-file guarded twins — both in pass-2 regions, both proving the fix is possible

The brief requires a named guarded twin. There are two, and they are better than the cross-file
`set_orig_class` one because they perform *the same MRO/type-dict lookup with the same hostile key* and
propagate:

1. **`_PySuper_LookupDescr:12622` (R37).** Walks `su_obj_type->tp_mro` and does
   `PyDict_GetItemRef(dict, name, &res)` per base — the same loop as `find_name_in_mro:6176-6191`, with the
   error **returned**, not cleared. Measured: `super().meth()` with a colliding key → `KeyboardInterrupt`
   propagates on all three builds. It even pins the MRO with the same `_PyCStackRef` idiom
   (`:12597-12601`), so the two functions are near-copies that diverge only on this one decision.
2. **`type_update_dict:6703` (R21).** `_PyDict_GetItemRef_Unicode_LockHeld(dict, name, old_value) < 0`
   → `return -1`. Measured: `setattr(C, 'meth', 1)` with a colliding key → `KeyboardInterrupt` propagates.

So within this one file, three code paths do the same lookup against the same hostile input and only
`find_name_in_mro` destroys the exception. The fix `find_name_in_mro` needs is the return convention its
two siblings already use.

**Verdict: CONSIDER**, per the guide's "unfiltered clear of a user-callback exception where the outcome is
a wrong-but-valid object". **Recommendation:** widen CPY-0074/CPY-0077 with (i) the no-warning `str`-subclass
trigger and (ii) the pickle/`copy` silent-data-loss consumers, and attach the two in-file twins as the fix
shape. Do **not** open a new record — the root cause is one line and it is already catalogued.

### 3d. One consumer that fails *safe* — noted so nobody re-flags it

`has_custom_mro:1268` (R6) calls `_PyType_LookupStackRefAndVersion` twice and compares. A cleared error
makes `c_ref1.ref` NULL, so `custom` reads as true and `type_mro_modified` takes `goto clear`, which
**disables the attribute cache for that type permanently** (`tp_versions_used = _Py_ATTR_CACHE_UNUSED`,
`:1317`). Wrong answer, conservative direction: a silent performance cliff, never a correctness bug.
**ACCEPTABLE.**

---

## 4. Sub-class (a) — destructor family: **0 clears, and the zero is earned**

`destructor_functions: 15` in the envelope. The scanner's family is name-based, so it includes three
functions that are **not** `tp_*` slots and that pass 1's slot-oriented enumeration did not name. All three
are pass-2 regions and all three were read for this report:

| function | region | read result |
|---|---|---|
| `managed_static_type_index_clear:255` | R3 | pure index write, no fallible call |
| `managed_static_type_state_clear:382-424` | R3 | asserts + atomics + `PyMutex_Lock/Unlock`; **no fallible API call at all**, so nothing to clear |
| `type_cache_clear:981-995` | R6 | `Py_XSETREF(entry->name, _Py_XNewRef(value))` under a seqlock write; infallible |
| `super_dealloc:12556-12565` | R37 | `Py_XDECREF` ×3 + `tp_free`; no exception state |
| `super_traverse:12973` | R37 | `Py_VISIT` only |

Together with pass 1's ten (`type_dealloc`, `type_dealloc_common`, `type_traverse`, `type_clear`,
`subtype_dealloc`, `subtype_clear`, `subtype_traverse`, `object_dealloc`, `slot_tp_finalize`,
`bufferwrapper_*`) that is the whole family. **Lexical `PyErr_Clear` inside a destructor: 0.**
Transitively reachable: 2 (`get_subclasses_key:9748`, `remove_subclass:9786`, via
`type_dealloc → type_dealloc_common → remove_all_subclasses`), and both are bracketed by
`type_dealloc_common:6853-6855`'s `PyErr_GetRaisedException()` / `PyErr_SetRaisedException()` pair with
`assert(!PyErr_Occurred())` at both ends of `remove_all_subclasses` (`:9800`, `:9808`).

**Zero, over a denominator of 15 destructor functions and 11 clears.** Pass 1's `slot_tp_finalize:11243`
CONSIDER stands and is not restated.

---

## 5. Sub-class (b) — success-path clears (the gh-146102 class): **0 of 11**

Each of the 11 clears was re-checked against the dominance test. Every one is dominated by a test of a
failure signal it caused itself:

| clear | dominating failure test |
|---|---|
| `2405`, `7490` | `type_module()` / `type_qualname()` returned NULL |
| `3023` | `_PyType_LookupRef` NULL **and** `ExceptionMatches(AttributeError)` |
| `6149` | `_PyObject_HashDictKey(name) == -1` |
| `6158` | `PyType_Ready(type) < 0` |
| `6183` | `ix == DKIX_ERROR` |
| `8619` | `PyDict_GetItemRef` < 0 **and** `!ExceptionMatches(AttributeError)` guard |
| `9748` | early-return guard `if (key != NULL) return key;` — the FP class named verbatim in the taxonomy |
| `9786` | `PyDict_DelItem(...)` non-zero |
| `10931` | `h == -1 && PyErr_Occurred()` |
| `11032` | `res == NULL && ExceptionMatches(AttributeError)` |

**No clear in this file is reachable on a path where the function set nothing.** The upstream
`mutablemapping_add_pairs` shape (`b3b7c0025be`, gh-146102) has no instance here. Genuine zero over 11.

---

## 6. Investigated and cleared (pass-2 regions, no finding)

- **R11 MRO C3 (3217-3702).** Zero `PyErr_Clear`. `class_name:3259` uses `PyObject_GetOptionalAttr`, which
  narrows to `AttributeError` internally, and propagates a `-1` correctly. `check_duplicates:3281` and
  `set_mro_error:3334` both bail out and let the user's exception through rather than overwriting it with
  their intended `TypeError` — the *right* trade. `mro_invoke:3591-3630` and `mro_internal:3655-3701`
  propagate every failure; a cleared error during linearisation leaving `tp_mro` inconsistent is **not**
  reachable via this agent's classes. *(Note for the refcount agent: the `Py_XNewRef`/`Py_XDECREF` pair at
  `:3665`/`:3668` looks like a double-drop against `:3696`/`:3698`, but `set_tp_mro:671` does a **plain
  store** and does not release the old value, so the balance is correct. Do not re-flag it.)*
- **R21 getattro (6529-6689).** Zero `PyErr_Clear`. `_Py_type_getattro_stackref`'s six `goto done` exits
  each propagate a descriptor `__get__` failure untouched; the `AttributeError` at `:6676` is set on the
  genuine not-found branch. The `suppress_missing_attribute` protocol is the documented narrow suppression
  and is correct.
- **R25 `__class__` assignment (7482-7846).** One clear (`7490`, `object_repr`) — recorded, CPY-0076.
  `o.__class__ = C` measured clean against the colliding-key input.
- **R26 pickle (7848-8406).** Zero `PyErr_Clear`. All `PyObject_GetOptionalAttr` / `PyDict_GetItemRef` uses
  test `< 0`. The region's exposure is entirely inherited from §3, not local.
- **R6 watchers (971-1481) / R3 managed static types (228-522).** Zero clears. `PyErr_FormatUnraisable:1223`
  is pass 1's §7 ACCEPTABLE (heap types are only freed by the GC, and the GC provably runs with an empty
  exception slot — `Python/gc.c:1989`, `:1436`); re-confirmed, not re-litigated.
- **R37 super (12534-13068).** Zero clears. `_PySuper_LookupDescr` and `supercheck:12733` both propagate;
  `_PySuper_LookupDescr` is guarded twin #1 in §3c.

---

## 7. Toolkit assessment

### Precision: 1/1 mechanically correct, 0/1 actionable

The single `pyerr_clear_unfiltered_after_python_call` fired on a genuine unfiltered clear after a call that
*can* reach `PyType_Ready`; it is downgraded to POLICY only by a documented contract plus two caller-side
asserts the scanner cannot see. `medium` confidence is the right calibration. **No rule change proposed.**
Suppression is also working: the `PyErr_ExceptionMatches` gate correctly hid `:3023`, `:8619`, `:11032`, and
the early-return-guard-clause FP class correctly hid `:9748` — 4 suppressions, all correct, all verified by
reading.

### Recall gap from pass 1: **CLOSED** — measure it and say so

Pass 1 reported `total_pyerr_clear_calls: 9` against a true count of 11, caused by `extract_functions`
desynchronizing on the `SLOT0`/`SLOT1`/`SLOT1BIN` statement macros and losing 686 lines. Re-running the
scanner at pass 2:

```
functions_analyzed: 459   (pass 1 era: 427 top-level functions were parseable)
total_pyerr_clear_calls: 11   <- was 9
```

**11 of 11.** Issue #28 / PR #29 recovered the region, and with it `slot_tp_hash:10931` and
`slot_tp_getattr_hook:11032` — the two clears that live in the functions that dispatch into arbitrary user
Python, i.e. exactly the population rule 3 exists to police. This is a confirmed, measurable recall win.

**Still outstanding from pass 1's proposal list:** no `total_pyerr_clear_calls_unattributed` canary in the
envelope. The reconciliation is currently manual (`grep -c` vs. the envelope, done in §0). It should be
mechanical — a non-zero value is the one signal that distinguishes "clean" from "parse artifact", and it
generalizes to every scanner that attributes sites to functions.

### **NEW recall gap — the rule that would have found §2**

**None of the three rules can see an unnarrowed exception *replacement*.** §2's defect has no `PyErr_Clear`
token anywhere near it, the fetch/restore balance is irrelevant, and there is no destructor. Yet its harm
is identical to rule 3's — a `MemoryError` is destroyed and replaced by a wrong, catchable exception with
`__context__ = None`. This is the same shape as two already-recorded findings, **CPY-0076** (`type_repr` /
`object_repr`) and **CPY-0078** (`same_slots_added`), which is evidence the class is recurrent in this file
and currently found only by hand.

**Proposed rule — `unnarrowed_exception_replacement`:**

> Flag a `PyErr_SetString` / `PyErr_Format` / `PyErr_SetObject` that sits inside the *failure branch* of a
> fallible call (the same branch shape rule 3 already computes), where no `PyErr_ExceptionMatches` dominates
> it and the branch does not first clear. Confidence `medium`; suppress when the branch is the
> "not found / absent" arm of a lookup rather than an error arm (i.e. when the dominating test is
> `== NULL` on a *tri-state* lookup whose zero case is documented).

Precision evidence from this slice: applied to the `_PyObject_SetAttributeErrorContext` family it yields
**exactly one hit in the whole tree** (`typeobject.c:6718`), with the guarded twin
(`object.c:2104`) sitting in the sibling file and the differential reproducing on three builds. Rule 3's
existing branch machinery supplies most of the implementation; only the "what is being *set*" test is new.

Two further gaps, recorded but **not** proposed as rules (both need cross-function reasoning that this
scanner deliberately does not do):

1. **The consumer side of a documented no-exception contract.** `find_name_in_mro` is POLICY at its own
   site (§1) and the harm lands three frames away in `pickle`/`copy` (§3b). No single-function rule can
   connect them. The pragmatic mitigation is a data-table entry: mark `_PyType_Lookup*` /
   `_PyObject_LookupSpecial` as *contractually exception-erasing*, so any agent triaging a **caller** knows
   a NULL from them may be a destroyed exception.
2. **Pass 1's restore-over-a-live-exception rule** (a `(_)PyErr_SetRaisedException` reachable on a path
   where a fallible call's failure return was not tested — `slot_tp_finalize:11243`) is still unimplemented.
   Pass 2 re-confirms it is the only defect of its kind here and that the other two restore sites both carry
   the `assert(!PyErr_Occurred())` that would be the fix, so it remains cheap and high-precision.

### Denominator honesty

11 clears, 15 destructor functions, 459 functions analyzed, 1 file. The zeros in §4 and §5 are over those
denominators, not over silence. The zero for sub-class (a) says nothing about (c) or (d) — and (d) is where
this pass's only FIX came from.

---

## Artifacts

All under `<scratchpad>/pass2_pyerr/`:
`probe3.py` (str-subclass reachability + consumer differential + the two guarded twins),
`probe4.py` (pickle round-trip silent data loss),
`probe5.py` (OOM sweep, indices 0-259, isolates the conversion to 1-4),
`probe6.py` (three-build type-vs-instance guarded-twin differential).
