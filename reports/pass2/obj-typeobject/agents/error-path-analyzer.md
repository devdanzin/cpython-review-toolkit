# Error Path Analysis Results — slice `obj-typeobject`, PASS 2

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scope:** `Objects/typeobject.c` (13,068 lines)
**Mode:** INFORMED — `RUN_CONTEXT.md`, `informed_briefing.md`, `PASS2_BASELINE.md`,
`include_map.md`, `history_context.md`, `scan_error_paths.sample.json`, and pass 1's
`reports/obj-typeobject/agents/error-path-analyzer.md` all read first.
**Builds:** `~/projects/python_build_matrix/builds/{debug,release}-{gil,ft}-nojit/python` @ `a1d580430c8`.
`git diff a1d580430c8 4f3be1b5777 -- Objects/typeobject.c Objects/dictobject.c Objects/object.c`
is **empty** — every file cited below is byte-identical between the matrix builds and the target ref.

**Scanner freshness:** re-ran `scan_error_paths.py` on the target file. 6 findings, identical to
`scan_error_paths.sample.json`. `grep -n PyErr_Clear` → 11 sites (2405, 3023, 6149, 6158, 6183,
7490, 8619, 9748, 9786, 10931, 11032), same as pass 1. Baseline is **not stale**.

---

## Summary

| | |
|---|---|
| Functions analyzed (scanner denominator) | **362** |
| `fallible_assignment_sites` | **45** |
| `int_status_callees_resolved` / `_discovered` | **116 / 86** |
| `unconditional_pyerr_clear` | 5 candidates → **0 net new** (4 confirm CPY-0076/CPY-0077, 1 ACCEPTABLE) |
| `int_status_never_tested` | 1 candidate → **1 confirmed** (= CPY-0070) |
| **New by reading** | **6 FIX, 9 CONSIDER** — every FIX reproduced on a matrix build |

Net-new findings. **All five FIX findings are reproduced**; two of the five CONSIDER findings are
statically decided and say so.

| # | verdict | site | symptom | build |
|---|---|---|---|---|
| §2a | FIX | `update_slot_after_setattr:6744` | `T.__dunder__ = v` succeeds with a live exception | SIGABRT debug-gil / SystemError release-gil |
| §2b | FIX | `update_all_slots:12177` | `T.__bases__ = (...)` ditto; in-tree comment asserts a falsehood; makes CPY-0070's cited twin dead code | SIGABRT debug-gil / SystemError release-gil |
| §2c | FIX | `type_set_bases_unlocked:1952+1968` | a *failed* `__bases__` assignment commits the MRO, method resolution, `issubclass` and both `__subclasses__()` | reproduced on FT (both) |
| §2d | FIX | `PyType_Watch:1111` | watcher reports success and is permanently silent; 3 lines of Python | reproduced on gil + ft |
| §4a | FIX | `set_mro_error:3320` (borrowed `to_merge` from `mro_implementation_unlocked:3497`) | **heap-use-after-free** — plain `class` stmt + metaclass `__hash__` | ASan UAF; SIGSEGV debug-gil + debug-ft |
| §4b | FIX | `object_getstate_default:7990` | `pickle.dumps` + `list.append` → **SIGSEGV** | 3/4 runs release-ft; ASan + TSan |
| §3 | CONSIDER | `_PyType_SetFlags:6453` | gh-148450's fix not applied to the sibling; invariant break reproduced, harm latent | reproduced on gil |
| §4 | CONSIDER | `managed_static_type_state_get:294` | `>` where `>=` is meant; one element past a 10-element array | static |
| §4a | CONSIDER | `mro_check:3547` vs the `mro[0] == type` asserts at `:5948`/`:6053` | **SIGABRT** from `defaultdict.__or__` after an evil `mro()` | reproduced debug-gil |
| §4a | CONSIDER | `type_set_bases_unlocked:1990` | `PyArg_UnpackTuple` status discarded; 7 guarded twins in-file | latent, not reachable today |
| §4a | CONSIDER | `mro_implementation_unlocked:3461`/`:3503` | two more borrowed-`tp_mro` windows across GC-tracked allocations | not reproduced |
| §4b | CONSIDER | `object_getstate_default:8010` | size-only guard → **silent slot loss** through pickle | reproduced on gil + ft |
| §4b | CONSIDER | `reduce_newobj:8230…8280` | `dumps` succeeds, `loads` fails — mismatched class name/payload | reproduced on gil |
| §4b | CONSIDER | `object_set_class_world_stopped:7757/7635/7688` | three `PyErr_Format`s inside the stop-the-world region | not reproduced |

The five `PyErr_Clear` candidates are pass 1's; they get one line each. **Pass 2's yield in this
lane came from job 2** — hunting the *class* behind `int_status_never_tested` rather than its one
instance. The class is real, it has three more members in this file, and two of them are
Python-reachable hard aborts on the default build.

---

## 1. The five `unconditional_pyerr_clear` candidates — error-return semantics

All five were triaged in pass 1. Confirmed, not re-litigated. My contribution is the
**error-return classification** the brief asked for: does the swallow leave the function returning
*success*, or returning *failure with nothing raised*?

| line | function | return on that path | exception state after | class | record |
|---|---|---|---|---|---|
| :2405 | `type_repr` | **success** (a valid repr string) | cleared | success-after-swallow | **CPY-0076** |
| :7490 | `object_repr` | **success** (a valid repr string) | cleared | success-after-swallow | **CPY-0076** |
| :6149 | `find_name_in_mro` | **-1 = failure** | cleared → *nothing set* | failure-without-exception | **CPY-0077** |
| :6158 | `find_name_in_mro` | **-1 = failure** | cleared → *nothing set* | failure-without-exception | **CPY-0077** |
| :9748 | `get_subclasses_key` | success (O(n) fallback) | cleared, `MemoryError` only | — | **ACCEPTABLE** |

`:9748` is the taxonomy's own "early-return guard clause" exemplar, named there by line number.
Dismissed. (Scanner should suppress it — pass 1's T3; still open.)

**The composite that only the error-return view shows.** `find_name_in_mro` has exactly **three**
call sites, all in this file (`:6361`, `:6365`, `:11942`). Its `-1`-with-nothing-set is then
converted *back into success* by `_PyType_LookupRefAndVersion:6369-6371`
(`*out = PyStackRef_NULL; return 0;` — "not found"). So the two halves of the error protocol are
broken in sequence: swallow → failure-without-exception → **success-with-a-wrong-answer**. That is
why CPY-0077's Python-visible symptom is `AttributeError` for an attribute that exists, and it
surfaces at the `T.x` entry point: `_Py_type_getattro_stackref:6674-6679` cannot distinguish
"absent" from "the lookup errored" and raises
`AttributeError: type object '%.100s' has no attribute '%U'`.

There is a **fourth** failure-without-exception return in the same function that no rule can see:
`:6165 if (mro == NULL) { return -1; }` — reached when `is_readying(type)` is true. No clear, no
raise, just the sentinel. It is inside the documented contract, so ACCEPTABLE, but note it is
invisible to every `PyErr_Clear`-keyed rule.

**New hazard created by §2 below.** `update_one_slot:11938` opens with
`assert(!PyErr_Occurred());` and the comment *"We may end up clearing live exceptions below, so
make sure it's ours."* That assertion is a **precondition** which §2's `update_all_slots` defect
violates on release builds — where the assert is compiled out and `find_name_in_mro`'s three clears
are then free to destroy the *caller's* live exception. The same precondition is asserted at
`:6351`. My `__bases__` reproducer aborts at exactly `:11938` on the debug build, which is the code
saying so itself.

---

## 2. `int_status_never_tested` — hunting the CLASS

**The recorded instance.** `type_set_bases_unlocked:1966`, `res = add_all_subclasses(...)` never
tested — **CPY-0070**. Confirmed, still present, one line, moving on.

**The class.** The shipped rule is built from `_INT_ASSIGN_TEMPLATE = r'(\w+)\s*=\s*({apis})\s*\('`
— it requires an **assignment**. A fallible int-status call written as a **bare expression
statement**, with the status discarded outright, is structurally invisible to it. I swept the file
for that shape over the same callee population the scanner discovers (154 `int`-returning functions
in the file, 104 admitted by the `'-1' in body` test, plus `INT_STATUS_APIS`), walking real
`expression_statement` nodes so a call inside a condition is not miscounted.

Seven sites. Four are real.

### [FIX] `update_slot_after_setattr` discards `update_slot`'s failure — GIL build only (typeobject.c:6744)

```c
static int
update_slot_after_setattr(PyTypeObject *type, PyObject *name)
{
#ifdef Py_GIL_DISABLED
    ...
    if (update_slot(type, name, &queued_updates) < 0) {   /* :6734 — the guarded twin */
        return -1;
    }
    ...
#else
    update_slot(type, name, NULL);                        /* :6744 — status discarded */
#endif
    return 0;                                             /* unconditional success */
}
```

**The guarded twin is the other arm of the same `#ifdef`, ten lines up.** That is as close as a
guarded twin gets.

**Why the discard is not safe.** `update_one_slot`'s header comment (`:11902-11905`) says it "will
only return an error if the `queued_updates` argument is provided and allocating memory for the
queue fails" — true of `update_one_slot`, and it is why `queued_updates == NULL` looks harmless.
But `update_slot` does not only call `update_one_slot`; it returns
`update_subclasses(type, name, update_slots_callback, &callback_data)` (`:12123`), and
`recurse_down_subclasses:12384` has an error return of its own that has nothing to do with the
queue:

```c
        int r = PyDict_Contains(dict, attr_name);   /* subclass tp_dict */
        if (r < 0) {
            Py_DECREF(subclass);
            return -1;
        }
```

`dict` is a *subclass's* `tp_dict`. If it holds a key that hash-collides with the dunder name and
compares with user code — a `str` subclass with a raising `__eq__`, exactly the mechanism pass 1
used for CPY-0074/CPY-0077 — `PyDict_Contains` returns `-1` with that exception live. So on the
default GIL build `update_slot` **can** fail, `update_slot_after_setattr` discards it, and
`type_setattro:6835` sets `res = 0` and returns success.

**Reproduced**, `scratchpad/p2_update_slot_after_setattr.py`:

```python
class S(str):
    def __eq__(self, other):
        if armed[0]: raise KeyboardInterrupt("EXC-FROM-USER-__eq__")
        return str.__eq__(self, other)
    __hash__ = str.__hash__

class Base: pass
Sub = type("Sub", (Base,), {S("__len__"): "decoy"})   # subclass dict, colliding key

armed[0] = True
Base.__len__ = lambda self: 7      # type_setattro -> update_slot_after_setattr -> :6744
```

| build | result |
|---|---|
| `debug-gil-nojit` | **SIGABRT (core dumped)** — `Objects/methodobject.c:409: Assertion '!_PyErr_Occurred(tstate)' failed` |
| `release-gil-nojit` | assignment returns **success**; the pending `KeyboardInterrupt` erupts as `SystemError: <built-in method write of _io.TextIOWrapper object ...> returned a result with an exception set` — attributed to an innocent bystander |
| `debug-ft-nojit`, `release-ft-nojit` | **`KeyboardInterrupt` propagates correctly** — the `:6734` arm |

The FT/GIL differential *is* the proof that the fix is already written.

**Fix.** Delete the `#ifdef`/`#else` asymmetry:
```c
#else
    if (update_slot(type, name, NULL) < 0) {
        return -1;
    }
#endif
```

**No prior art.** `gh api search/issues` on `repo:python/cpython` for
`type_setattro returned a result with an exception set`, `update_slot exception __eq__ subclass dict`,
`setattr dunder pending exception SystemError typeobject` — all 0 results.

---

### [FIX] `update_all_slots` discards the same failure, and its comment asserts a falsehood (typeobject.c:12177)

```c
#else
// Called when __bases__ is re-assigned.
static int
update_all_slots(PyTypeObject* type)
{
    pytype_slotdef *p;
    for (p = slotdefs; p->name; p++) {
        /* update_slot returns int but can't actually fail in this case*/
        update_slot(type, p->name_strobj, NULL);          /* :12177 */
    }
    return 0;
}
#endif
```

The comment is **false**, for the reason above. Guarded twin: the `#ifdef Py_GIL_DISABLED` arm at
`:12144-12167`, which propagates `-1` *and* frees the queue chunks.

**Two consequences, both worse than the `setattr` case.**

1. The loop **keeps going** after the failure, calling `update_one_slot` for every remaining
   slotdef with a live exception — violating that function's own `assert(!PyErr_Occurred())`
   precondition at `:11938`.
2. **It makes CPY-0070's cited guarded twin dead code.** CPY-0070's report names `:1967`
   (`if (update_all_slots(type) < 0) { goto bail; }`, added 2025-05-27) as the correctly-handled
   next line. On the default build `update_all_slots` can never return `< 0`, so that test never
   fires and the `bail:` rollback is unreachable from there.

**Reproduced**, `scratchpad/p2_update_all_slots.py` (`X.__bases__ = (B,)` with a colliding key in a
subclass dict):

| build | result |
|---|---|
| `debug-gil-nojit` | **SIGABRT (core dumped)** — `Objects/typeobject.c:11938: Assertion '!PyErr_Occurred()' failed` in `update_one_slot`, i.e. consequence 1 |
| `release-gil-nojit` | assignment returns **success**; `SystemError: ... returned a result with an exception set` |
| `debug-ft-nojit`, `release-ft-nojit` | `KeyboardInterrupt` propagates correctly |

**Prior art — direct, and closed unmerged.** **bpo-38554 / PR gh-16879** (2019),
*"Fix a possible assertion failure in test_descr"*, reports exactly this assertion:
*"`update_all_slots()` was being called immediately after `add_all_subclasses()`. This would cause
an assertion failure in `update_all_slots()` if an exception occurred during
`add_all_subclasses()`."* Same assertion, different upstream. That PR's reordering would **not**
have fixed this input, because here the live exception originates *inside* `update_slot` itself.
The issue is 7 years old and the assertion is still reachable.

---

### [FIX] `type_set_bases_unlocked`'s `bail:` is a partial rollback, and the rollback log is already freed (typeobject.c:1952 + :1968 → :2000)

Found while reading the FT output of the previous reproducer. `type_set_bases_unlocked` has two
error labels:

```c
    if (mro_hierarchy_for_complete_type(type, temp) < 0) {
        goto undo;                         /* undo: restores every cls's old tp_mro */
    }
    Py_DECREF(temp);                       /* :1952 — the rollback log is RELEASED here */
    ...
        remove_all_subclasses(type, old_bases);
        res = add_all_subclasses(type, new_bases);
        if (update_all_slots(type) < 0) {
            goto bail;                     /* :1968 — bail: restores tp_bases/tp_base ONLY */
        }
```

`undo:` (`:1984-1998`) walks `temp` — the list of `(cls, new_mro, old_mro)` triples — and restores
every affected class's MRO, then falls through into `bail:`. But `temp` is `Py_DECREF`'d at `:1952`
the instant `mro_hierarchy_for_complete_type` succeeds. By the time control reaches `:1968`, the
information needed to undo the MRO commit **no longer exists**, so `bail:` cannot restore it even
in principle. It restores `tp_bases` and `tp_base` and nothing else.

**Reproduced on the free-threaded build**, `scratchpad/p2_bases_mro_desync.py` — i.e. on the build
where the `:1967` guard *does* work, so the error is correctly raised and the rollback is genuinely
taken:

```
assignment: raised KeyboardInterrupt: EXC-FROM-USER-__eq__   <-- error correctly propagated

  X.__bases__        = (<class 'A'>,)                        <-- rolled back
  X.__mro__          = (<class 'X'>, <class 'B'>, <class 'object'>)   <-- NOT rolled back
  Sub.__mro__        = (<class 'Sub'>, <class 'X'>, <class 'B'>, <class 'object'>)

  X().who()          = B          <-- methods resolve through the rejected base
  issubclass(X, A)   = False
  issubclass(X, B)   = True
  isinstance(X(), B) = True
  B.__subclasses__() = [<class 'X'>]        <-- subclass bookkeeping also not rolled back
  A.__subclasses__() = []
```

Identical on `release-ft-nojit` and `debug-ft-nojit`. A **failed** `__bases__` assignment leaves
`__bases__` as the only thing restored; the MRO, method resolution, `issubclass`, `isinstance` and
both parents' `__subclasses__()` all keep the rejected value, permanently and silently.

**Why this matters beyond FT.** The obvious fix for CPY-0070 — test `add_all_subclasses` at `:1966`
and `goto bail` — would make this desync reachable on the **default GIL build** as well. The two
defects currently mask each other: the dead `:1967` guard is the only thing keeping `bail:` from
being taken after the MRO commit. **Fix CPY-0070 and this one together, or the CPY-0070 fix is a
regression.** The correct shape is to keep `temp` alive until the last failure exit and route
`:1968` (and any new `:1966` test) through `undo:`.

Guarded twin: `undo:` itself, 16 lines below — the same function already knows how to do this.

---

### [FIX] `PyType_Watch` discards `assign_version_tag`'s status and returns success (typeobject.c:1111)

This one is invisible to the rule for a **second** reason: `assign_version_tag` uses a 0/1 status
("Return 0 if this cannot be done, 1 if `tp_version_tag` is set", `:1414`), so the scanner's
`'-1' in body` discovery test excludes it from the callee population entirely.

```c
int
PyType_Watch(int watcher_id, PyObject* obj)
{
    ...
    // ensure we will get a callback on the next modification
    BEGIN_TYPE_LOCK();
    assign_version_tag(interp, type);          /* :1111 — status discarded */
    type->tp_watched |= (1 << watcher_id);
    END_TYPE_LOCK();
    return 0;                                  /* unconditional success */
}
```

The comment states the postcondition; the discarded call is the only thing that establishes it.

**Why it fails, and why it is permanent.** `assign_version_tag` returns 0 at `:1420` (not READY),
`:1424` (`tp_versions_used >= MAX_VERSIONS_PER_CLASS`, which is **1000**), `:1432` (any base could
not be assigned), `:1440`/`:1449` (global tag exhaustion). `tp_versions_used` only ever increments
(`set_version_unlocked:1147`), so the 1000-cap is **monotonic and irreversible**.

And `_PyType_Modified_Unlocked:1189` early-returns on `tp_version_tag == 0` — while the watcher
notification loop is at `:1211-1231`, *after* that return. So a type that cannot hold a version tag
**never notifies its watchers**, and `PyType_Watch` reported success with no exception set.

**Reproduced**, `scratchpad/p2_type_watch_silent.py` and `p2_type_watch_pure.py`:

```
CONTROL  watch_type(wid, Healthy);  Healthy.foo = "bar"
         events: [<class 'Healthy'>]                        <-- watcher fires

(a) after 1100 rounds of ordinary `C.attr = i; C.attr`      <-- pure Python, no _testcapi
    assign_version_tag -> 0
    watch_type(wid, PureVictim) returns without error
    events after modification: []                           <-- permanently silent

(b) ExhaustedBase assign_version_tag -> 0
    class FreshChild(ExhaustedBase): ...                    <-- brand new, zero mutations
    FreshChild assign_version_tag -> 0                      <-- poisoned via :1431 recursion
    events on the FRESH subclass after 2 modifications: []
```

Identical on `release-gil-nojit`, `debug-gil-nojit`, and `release-ft-nojit`. The version budget is
burned by **ordinary attribute traffic** — `_testcapi` is used only to install the watcher and read
the event list, which is the only way to observe a C-API watcher from Python at all. And because
`assign_version_tag` recurses over the bases first, **one exhausted base class silently disables
watchers on every subclass, including subclasses created afterwards.**

**The cheap trigger — three lines, no budget exhaustion needed.** `type_mro_modified:1296` does
`if (!Py_IS_TYPE(type, &PyType_Type) && has_custom_mro(type)) goto clear;`, and `clear:` sets
`type->tp_versions_used = _Py_ATTR_CACHE_UNUSED;` (`:1317`) — **permanently**.
`assign_version_tag:1422`'s own comment says so: *"(this includes `tp_versions_used ==
_Py_ATTR_CACHE_UNUSED`)"*. So **any class with a custom metaclass `mro()` is permanently
unwatchable**, and `PyType_Watch` still returns success
(`scratchpad/p2_type_watch_custom_mro.py`, `release-gil-nojit`):

```
class Meta(type):
    def mro(cls): return type.mro(cls)
class Watched(metaclass=Meta): pass

Plain (control)        assign_version_tag=1  events after 2 modifications: [<class 'Plain'>]
Watched (custom mro)   assign_version_tag=0  events after 2 modifications: []
Child(Watched)         assign_version_tag=0  events: []
```

The same applies to any type whose MRO contains a non-supertype (`type_mro_modified:1308`) — i.e.
the whole `gh-127773 "Disable attribute cache on incompatible MRO entries"` population. This is the
reachable form of the finding; the 1000-mutation route above is the exotic one.

**Guarded twin — same callee, same file, 53 lines from `_PyType_SetFlags`.** `_PyType_Validate:6471`:

```c
    if (assign_version_tag(_PyInterpreterState_GET(), ty)) {
        *tp_version = ty->tp_version_tag;
    }
    else {
        err = -1;              /* <-- converts the 0 into an error return */
    }
```

Of the four non-recursive call sites, three handle the status — `:1462`
`PyUnstable_Type_AssignVersionTag` returns it, `:6359` recovers it from `type->tp_version_tag` and
tests it at `:6374`, `:6471` converts it to `-1`. Only `:1111` drops it.

**Fix.** `if (!assign_version_tag(interp, type)) { END_TYPE_LOCK(); PyErr_SetString(PyExc_RuntimeError, "cannot watch a type that has exhausted its version tags"); return -1; }` — or at minimum document that `PyType_Watch` succeeding does not guarantee callbacks. `PyType_Watch` is public C API documented to return `-1` on error.

**No prior art.** Tracker searches for `PyType_Watch version tag`,
`type watcher not called MAX_VERSIONS_PER_CLASS`, `assign_version_tag PyType_Watch` return only
gh-97875 (the API's own introduction) and unrelated issues.

---

### The three dismissals in this class (with reasons)

| line | site | verdict |
|---|---|---|
| `:12136` | `fixup_slot_dispatchers` → `update_one_slot(type, p, &p, NULL)` | **ACCEPTABLE.** Confirms pass 1's dismissal, now with the mechanism: this calls `update_one_slot` *directly*, not `update_slot`, so `recurse_down_subclasses`/`PyDict_Contains` is never reached and `queued_updates == NULL` really does make `-1` unreachable. Not the same as `:6744`/`:12177`. |
| `:6359` | `_PyType_LookupStackRefAndVersion` → `assign_version_tag` | **ACCEPTABLE.** The status is recovered from the field on the very next line (`version_tag = type->tp_version_tag;`) and tested at `:6374` (`if (version_tag == 0 ... ) return 0;`). |
| `:10024` | `wrap_sq_item` → `check_num_args(args, 1)` | **ACCEPTABLE.** The branch is reached only when `PyTuple_GET_SIZE(args) != 1`, so the call is guaranteed to fail and is being used *purely to set the exception*; the next two lines are `assert(PyErr_Occurred()); return NULL;`. A new FP class — see §6. |

---

## 3. Job 3 — the gh-148450 sibling gap: `_PyType_SetFlags` vs `_PyType_SetFlagsRecursive`

**[CONSIDER] `_PyType_SetFlags:6453` mutates `tp_flags` with no version-tag invalidation.**

`e7eaed56149a` (2026-05-23, gh-148450, + three backports gh-150300/150307/150308) added
`_PyType_Modified_Unlocked(self)` to `_PyType_SetFlagsRecursive:6506` and a
`BEGIN_TYPE_LOCK()` / `type_lock_prevent_release()` bracket around its stop-the-world. Its sibling
53 lines earlier still reads in full:

```c
void
_PyType_SetFlags(PyTypeObject *self, unsigned long mask, unsigned long flags)
{
    unsigned long new_flags = (self->tp_flags & ~mask) | flags;
    if (new_flags != self->tp_flags) {
        types_stop_world();
        // can't use new_flags here since they could be out-of-date
        self->tp_flags = (self->tp_flags & ~mask) | flags;
        types_start_world();
    }
}
```

No invalidation, no `TYPE_LOCK`, no `type_lock_prevent_release`. Both are called from
`Modules/_abc.c` — `:541` (the `__abc_tpflags__` path in `_abc_init`) and `:601` (the
`abc.register` path gh-148450 fixed).

### Verdict: the invariant break is real and reproducible; the harm is currently latent.

**(a) Single-threaded, it is normally masked — but not always.** `_abc__abc_init_impl` runs, in
order: `compute_abstract_methods` → `PyObject_SetAttr(self, _abc_impl, data)` (`:510`, a
`type_setattro` that *does* invalidate) → `PyDict_Pop(tp_dict, __abc_tpflags__)` (`:521`) →
`_PyType_SetFlags` (`:541`). Step 2 incidentally zeroes the version tag, so the plain transcription
of gh-148450's regression test onto this path **passes** (`scratchpad/p2_setflags_sibling.py`:
`Target ver=0`, `TargetChild ver=0`).

Step 3 opens a window. `PyDict_Pop` on the type dict runs a user `__eq__` if that dict holds a
`str`-subclass key hash-colliding with `__abc_tpflags__`, and that user code can re-assign a
version tag. **Reproduced**, `scratchpad/p2_setflags_window.py`, on `release-gil-nojit` and
`debug-gil-nojit`:

```
lying __eq__ fired during _abc_init: 1 time(s)
version AFTER _abc_init:            131160
tp_flags really changed (MATCH_MAPPING now true): True
VIOLATION: tp_flags changed while version tag 131160 stayed LIVE.
```

That is precisely the state `Lib/test/test_type_cache.py::test_abc_register_invalidates_subclass_versions`
asserts must not occur.

**(b) The downstream harm is latent today.** gh-148450's stated mechanism is *"JIT won't invalid the
old trace"*. I checked: `Python/optimizer_cases.c.h:3525/3535` handles `_MATCH_MAPPING` and
`_MATCH_SEQUENCE` with `res = sym_new_not_null(ctx)` — **no constant folding** — and the uops in
`Python/executor_cases.c.h:14272+` still read `tp_flags` at runtime. `Py_TPFLAGS_MAPPING` /
`Py_TPFLAGS_SEQUENCE` have **no version-guarded consumer** in the tree today. So the stale-trace
consequence is not observable at this commit.

**(c) The free-threaded exposure is the sharper half, and is not masked.** The twin's own comment
(`:6519-6520`) says *"Keep `TYPE_LOCK` held while waiting for stop-the-world so no thread can
reassign a version tag before the flag update."* `_PyType_SetFlags` holds no lock at all, so a
concurrent `_PyType_Lookup` on another thread can assign a fresh version tag in the window between
step 2's invalidation and step 4's flag write. That is the exact race the twin was hardened
against, on the sibling that was not.

**Classification: CONSIDER, not FIX** — the invariant violation is reproduced, but no consumer
exploits it at this commit. It should be fixed for the same reason gh-148450 was: the invariant is
load-bearing enough to have earned a fix plus three backports two months ago, and the fix was
applied to one of the two mutation paths because that was the one in the bug report. The stated bug
is a property of *mutating flags*, not of *recursing*.

**Fix.** Give `_PyType_SetFlags` the same body shape as `_PyType_SetFlagsRecursive:6508-6526`.

---

## 4. Other reads — regions R3, R19, R21, R6 (read directly)

**[CONSIDER] `managed_static_type_state_get:294` — `>` where `>=` is meant (R3).**

```c
    size_t index = managed_static_type_index_get(self);
    managed_static_type_state *state = &(interp->types.builtins.initialized[index]);
    if (state->type == self) {
        return state;
    }
    if (index > _Py_MAX_MANAGED_STATIC_EXT_TYPES) {      /* :294 — off by one */
        return state;
    }
    return &(interp->types.for_extensions.initialized[index]);
```

`_Py_MAX_MANAGED_STATIC_EXT_TYPES` is **10** (`pycore_interp_structs.h:544`) and
`for_extensions.initialized` is a 10-element array (`:650`). `index == 10` passes `10 > 10` and
falls through to `&initialized[10]` — one element past the end. The index is produced by
`managed_static_type_state_init:324-327`, which does `index = next_index++` with **no runtime
bound** — only `assert(index < _Py_MAX_MANAGED_STATIC_EXT_TYPES)`, which vanishes on release.

Reachable only via `_PyStaticType_InitForExtension`, whose sole in-tree caller is
`Modules/_datetimemodule.c:7531` — and that registers **6 of the 10 slots** (`capi_types[]`). So
60% of the budget is consumed by one stdlib module and the overflow is assert-only. Not reachable
from pure Python; an embedder or third-party extension registering five more managed static types
reaches it. Same family as **CPY-0089**'s third site (`:709`, the unasserted `state->` deref) but a
distinct defect — CPY-0089 is about the consumer, this is about the bound itself.

**Clean negatives in my lane, with denominators:**

- **`_Py_type_getattro_stackref:6571-6689`** (NET NEW candidate `:6620`, `publish_before_init_complete`,
  confidence `low`) — **clean**. Three `_PyCStackRef`s, six `goto done` exits; every exit funnels
  through the single `done:` label which pops all three in order. The only two early returns
  (`:6581`, `:6587`) are *before* the pushes. The two hand-offs (`:6650-6651`, `:6669-6670`) NULL
  the source after transferring. Every failed descriptor call leaves `result_ref.ref` as
  `PyStackRef_NULL` with the descriptor's exception set. The one sentinel-without-exception return
  (`:6675-6683`, `*suppress_missing_attribute = 1`) is the documented contract
  (`:6564-6568`) and the `tp_getattro` entry point `_Py_type_getattro:6557` passes `NULL`, so the
  exception is always set on that path.
- **`_PyType_Validate:6464-6480`** returns `-1` with no exception set on the `assign_version_tag`
  failure branch. Its **only** caller is `Python/specialize.c:2863`, which treats `err < 0` as
  `SPECIALIZATION_FAIL(TO_BOOL, SPEC_FAIL_OUT_OF_VERSIONS); goto failure;` — specialization misses
  never raise. **ACCEPTABLE**, contract honoured.
- **Preprocessor-arm asymmetry sweep.** All 30 `Py_GIL_DISABLED` conditional groups in the file
  were listed and the four with a non-trivial `#else` arm inspected (`:1137/:1149`, `:1346/:1348`,
  `:3789/:3929`, `:6311/:6338`, `:6728/:6743`, `:12144/:12172`). Only **two** have asymmetric error
  handling and both are reported above. `:3929` is a `typedef void slot_update_t;` stub;
  `:6311/:6338` are the seqlock vs GIL cache-read paths, both complete.

---

## 4a. Delegated region read — R11 MRO / C3 linearisation (3217–3702)

Denominator: **24 functions read in full** (R11's eleven, the four callers, nine supporting),
~48 fallible/status call sites checked.

### [FIX] `set_mro_error:3320` reads a borrowed `tp_mro` freed by its own previous iteration — heap-use-after-free, reproduced

`mro_implementation_unlocked` builds the C3 input array out of **borrowed** references:

```c
3488  PyObject **to_merge = PyMem_New(PyObject *, n + 1);
3497      mro_to_merge = lookup_tp_mro(base);   /* BORROWED — no incref */
3499      to_merge[i] = mro_to_merge;
3501  to_merge[n] = bases;                      /* BORROWED */
3510  if (pmerge(result, to_merge, n + 1) < 0)
```

`pmerge:3420` → `set_mro_error(to_merge, …)`, whose first loop re-reads `to_merge[i]` **after**
running arbitrary Python:

```c
3318  for (i = 0; i < to_merge_size; i++) {
3319      PyObject *L = to_merge[i];
3320      if (remain[i] < PyTuple_GET_SIZE(L)) {          /* <-- UAF READ */
3321          PyObject *c = PyTuple_GET_ITEM(L, remain[i]);
3322          if (PyDict_SetItem(set, c, Py_None) < 0) {  /* c is a class -> metaclass __hash__ */
```

`c` is a **class**, so `PyDict_SetItem` hashes it through the metaclass's `__hash__`. That user code
reassigns `__bases__` somewhere in the graph, `set_tp_mro:671` raw-stores the new MRO, and the old
tuple is released — leaving `to_merge[i]` dangling for iteration `i+1`. **The free site is one line
below the read, inside the same loop.**

**Independently re-verified by me:**

| build | script | result |
|---|---|---|
| `debug-gil-nojit-asan` | `p1c_asan.py` | **`heap-use-after-free`, READ of size 8** at `set_mro_error typeobject.c:3320` ← `pmerge:3420` ← `mro_implementation_unlocked:3510` ← `mro_implementation:3523`; **freed at `set_mro_error typeobject.c:3322`** |
| `debug-gil-nojit` | `p1d_segv.py` | **SIGSEGV (core dumped), rc=139** |
| `release-gil-nojit` | `p1d_segv.py` | rc=0 — survives; the block is not recycled at this allocation timing. **Not an exoneration** (the taxonomy's "clean sweep is not an exoneration" rule); the sub-read reports `release-gil-nojit-asan` showing the same use-after-free at `:3320` *and* `:3322` |
| `debug-ft-nojit`, `debug-ft-nojit-asan` | `p1c_asan.py` | SIGSEGV — the FT type lock does **not** block this, because the re-entrancy is on the same thread |

No C extension, no threads, no `ctypes` — a plain `class` statement, a metaclass with an ordinary
`__hash__`, and a documented `__bases__` assignment. The full ASan stack bottoms out in
`__build_class__` → `type_new` → `PyType_Ready` → `type_ready_mro:9259` → `mro_internal:3666` →
`mro_invoke:3603` → `type_mro:3539` → `mro_implementation_unlocked:3510` → `pmerge:3420` →
`set_mro_error:3320`.

**Guarded twins — three, all in this file, all protecting the same field against the same hazard:**

| line | what it does |
|---|---|
| `:3662-3665` `mro_internal` | *"Keep a reference … **Don't let old_mro be GC'ed and its address be reused**"* → `old_mro = Py_XNewRef(lookup_tp_mro(type));` — the identical pointer held across the identical re-entrant call, refcount-protected |
| `:6170` `find_name_in_mro` | *"Keep a strong reference to mro because `type->tp_mro` can be replaced during dict lookup"* |
| `:12597` `_PySuper_LookupDescr` | same comment for `su_obj_type->tp_mro` |

`mro_implementation_unlocked:3497` is **the only place in the file that stashes `tp_mro` across a
Python-running call without a strong ref**. `class_name:3262` even carries the comment
*"Note that this is potentially re-entrant."* — the file knows.

This resolves the NET-NEW baseline candidate `:3443`
(`mro_implementation_unlocked`, `borrowed_field_deref_across_call`, confidence `high`) — the
discriminator `include_map.md` posed (*"`TYPE_LOCK` blocks another thread; it does not block a
re-entrant `T.__bases__ = …` on this thread"*) resolves **against** safety, and the actual crash is
one frame deeper than the flagged line, in `set_mro_error`.

**Fix.** Make `to_merge` own its entries: `Py_INCREF` each `lookup_tp_mro(base)` and `bases` at
`:3495-3501`, release alongside `PyMem_Free(to_merge)` at `:3505`/`:3513`. C3 needs a consistent
snapshot anyway. Worth hardening `set_mro_error` in the same patch — `type->tp_name` instead of
`class_name()`, and a pointer-identity list instead of a class-keyed dict, which removes *all*
re-entrancy from the error path.

### [CONSIDER] `mro_check:3547` does not enforce `mro[0] == type`, but two token APIs assert it — reproduced SIGABRT

`mro_check` validates only that every element is a type and that layouts are compatible. An evil
metaclass `mro()` still gets these past it (`p2_mrocheck_gaps.py`, `debug-gil-nojit`):
`[object, cls]` **accepted** (`C.__mro__ == (object, C)`), `[cls, cls, cls, object]` **accepted**,
`[cls]` with `object` absent **accepted**.

Yet `PyType_GetModuleByToken_DuringGC:5948` and `PyType_GetBaseByToken_DuringGC:6053` both
`assert(PyTuple_GET_ITEM(mro, 0) == (PyObject *)type);` under the comment
*"mro_invoke() ensures that the type MRO cannot be empty … the first item in the MRO is the type
itself"*. The `size >= 1` half **is** enforced (`mro_invoke:3619`); the `mro[0] == type` half is
enforced nowhere. The comment attributes a guarantee to `mro_invoke` that it never makes.

**Independently re-verified**: `p4_assert_mro0.py` on `debug-gil-nojit` →
`Objects/typeobject.c:6053: Assertion 'PyTuple_GET_ITEM(mro, 0) == (PyObject *)type' failed`,
**rc=134 (SIGABRT)**, reached from pure Python via `collections.defaultdict.__or__`
(`Modules/_collectionsmodule.c:2417`). `_decimal` reaches the same assert via
`Modules/_decimal/_decimal.c:150/165/169`.

CONSIDER, not FIX, per the triage rules: assert-only, so release builds do not crash — they
silently skip index 0 and can report "no such base" for a token that is present at `mro[0]`. But the
value *can* be bad and the site *is* reachable, so this is a real CI abort. Distinct from CPY-0089
(latent assert-only guards) precisely because it is **falsifiable and reached**.

**Fix.** Drop the false attribution and make both call sites start at `i = 0` with a real check
instead of the assert. (Adding the check to `mro_check` would reject MROs Python has historically
accepted.)

### [CONSIDER] `type_set_bases_unlocked:1990` — `PyArg_UnpackTuple` status discarded on the `undo:` path

A bare expression statement; `cls` and `new_mro` are **uninitialised** (only `old_mro` is pre-set to
`NULL`), so on failure `:1993` would dereference an uninitialised stack pointer — and the exception
`PyArg_UnpackTuple` sets would clobber the original error that drove `goto undo`, which is never
cleared before `return -1` at `:2020`.

**Seven guarded twins in this same file**, all `if (!PyArg_UnpackTuple(...))`: `:10037`, `:10093`,
`:10190`, `:10300`, `:10321`, `:10350`, `:10371`. This site is the sole outlier.

**Not reachable today**: `temp` is populated only at `mro_hierarchy_for_complete_type:1820` with
tuples built at `:1813`/`:1816`, so the `2..3` arity always holds and the list is never exposed to
Python. Latent — one refactor from a wild pointer deref. Note this is a **third** instance of the
`discarded_int_status` shape (§6), in a rule-visible API (`PyArg_UnpackTuple`), that the shipped
`unchecked_parse` rule did not flag.

### [CONSIDER, not reproduced] Two more borrowed-`tp_mro` windows in the same function

`:3503 PyList_New(1)` (after `to_merge[]` is populated, before `pmerge`) and `:3461 PyTuple_New(k+1)`
in the `n == 1` fast path with `base_mro` borrowed at `:3458` — both GC-tracked allocations that can
trigger a gen-0 collection → `gc.callbacks` / `__del__` → arbitrary Python. The second is the
single-inheritance path taken by essentially every `class` statement, and it would make the bug
reachable on the **success** path, not only the MRO-conflict error path. The sub-read could not aim
a collection at those exact allocations (`p3_gc_window.py`: callback fired, window missed) and
reports it honestly as unreproduced. The FIX above closes both.

### Clean negatives in R11, with denominators

- **The three-valued `mro_internal` contract — all callers correct.** Tree-wide grep finds exactly
  **two**: `mro_hierarchy_for_complete_type:1803` (`if (res <= 0) return res;` — correctly
  distinguishes `0` = re-entered from `-1` = error) and `type_ready_mro:9259` (`if (... < 0)`).
  No caller uses the `if (!mro_internal(...))` form. **Clean.**
- **`mro_internal`'s re-entrancy strong ref balances on all three exits.** The apparent
  double-release (`Py_XDECREF(old_mro)` at `:3668` and again at `:3696`/`:3698`) is correct because
  `set_tp_mro:671` is a raw store that does not decref the previous value: two increfs, two decrefs.
  **Clean** — and worth recording so nobody re-flags it.
- **Raw allocator pairing — both pairs correct on every `goto`/early return, and both failure
  branches raise.** Checked explicitly because `PyMem_New` is a plain macro over `PyMem_Malloc`
  (`Include/pymem.h:63`) and does not raise: `remain` (`pmerge:3371` → `PyErr_NoMemory():3373`,
  freed at `:3425`, no path escapes) and `to_merge` (`:3488` → `PyErr_NoMemory():3490`, freed at
  `:3505`/`:3513`, no path escapes).
- **`set_mro_error` always leaves an exception set** on all three early returns (`:3316`, `:3322`,
  `:3344`), so `pmerge:3421` never returns `-1` with nothing raised. The exception may be a
  *different* one than the intended `TypeError` (a metaclass `__name__` property raising
  `ValueError` substitutes it) — message substitution, not a sentinel-without-exception bug.
  ACCEPTABLE. The `buf` arithmetic was also checked: `PyOS_snprintf` does **not** clamp its return
  (`Python/mysnprintf.c` returns the C99 would-be length) so `off` can exceed `sizeof(buf)`, but the
  guards at `:3333` and `:3351` bound every write. No overflow.
- **`mro_invoke`'s error path is refcount-clean.** All four exits drop exactly what they own;
  `Py_DECREF(new_mro)` precedes `PyErr_Format` at `:3620-3621` (the safe order, so a finaliser
  cannot clobber the exception); no stale `tp_mro` is committed because `mro_internal:3667`'s
  identity check runs *after* `mro_invoke` returns.
- **Not findings, verified:** `type_mro_modified:1278` and `_PyType_Modified_Unlocked` both return
  `void`, so their bare-statement call sites at `:3680`/`:3683`/`:3687` are **not** discarded
  statuses — a required suppression for the §6 rule proposal.

---

## 4b. Delegated region reads — R25 `__class__` assignment / R26 pickle

Two sub-reads were run in parallel over the regions the brief named. Denominators: R25 **11
functions / 14 fallible call sites**, R26 **12 functions / 39 fallible call sites** — all 53
checked for sentinel-without-exception, success-with-live-exception, and discarded int status.

### [FIX] `object_getstate_default:7990` — `PyList_GET_ITEM` on a Python-published list → SIGSEGV (FT)

```c
    name = Py_NewRef(PyList_GET_ITEM(slotnames, i));
```

`slotnames` is not private: `_PyType_GetSlotNames:7874` returns the very list stored as
`cls.__slotnames__` in the class dict, reachable and mutable from every thread. `PyList_GET_ITEM`
does no bounds check and no atomic load, so a concurrent `list_resize` (an ordinary `list.append`)
republishes `ob_item` under the reader and `Py_NewRef` faults on it.

The function's own comment at `:8008-8009` — *"The list is stored on the class so it may mutate
while we iterate over it"* — shows the hazard was known; the mitigation written (the size guard at
`:8010-8014`) is a **same-thread** guard and does nothing about another thread.

**Independently re-verified by me**, not just by the sub-read:
`scratchpad/min_repro_7990.py`, 6 reader threads doing `pickle.dumps(o, 2)` + 2 writer threads
doing `S.__slotnames__.append("q"); .pop()` —

| build | outcome |
|---|---|
| `release-ft-nojit` | **3 of 4 runs SIGSEGV (core dumped), rc=139** |
| `release-gil-nojit` | clean, rc=0 (FT-only, as expected) |
| `debug-ft-nojit-asan` | `SEGV on unknown address 0x0c` — `ob_ref_local` off a NULL `PyObject *`, stack `Py_INCREF ← _Py_NewRef ← object_getstate_default:7990` |
| `debug-ft-nojit-tsan` | `data race Objects/typeobject.c:7990:20 in object_getstate_default` vs `list_resize Objects/listobject.c:165:6` |

Entry point is pure Python — `pickle.dumps(obj)` on one thread, `list.append` on another. No C
extension.

**Guarded twin.** `PyList_GetItemRef` (26 call sites tree-wide) exists for exactly this;
`Objects/genericaliasobject.c:71` is the structural twin. **Within this file the contrast is
total**: the other three `PyList_GET_ITEM` sites (`:1853`, `:1990`, `:6499`) all iterate *private*
lists (`_PyType_GetSubclasses()` output, or the local `temp` accumulator) that no other thread can
reach. `:7990` is the only one over a Python-reachable list.

**Fix.** Snapshot before the loop — this kills the next finding too:
`PyObject *snap = PyList_AsTuple(slotnames); ... Py_SETREF(slotnames, snap);` then
`PyTuple_GET_ITEM`, and delete the now-unreachable `:8008-8014` size guard.

**No prior art.** `gh api search/issues` for `__slotnames__ race pickle free-threading` → 0;
`object_getstate_default` → 1 hit, gh-115747, unrelated (`__slots__` as an iterable).

### [CONSIDER] `object_getstate_default:8010` — the size guard is ineffective, and the miss is silent

The brief's item 5, answered precisely: **neither a crash nor merely cosmetic — silent data loss**,
on the GIL build too. The guard sits at the *end* of the loop body, after `PyObject_GetOptionalAttr`
(`:7991`) and `PyDict_SetItem` (`:8000`) have both run user code, so a **size-preserving** mutation
slips through.

Severity bounded by measurement, not assumption:
- **Not OOB** — the guard does dominate the next iteration's read, so `i < PyList_GET_SIZE` always
  holds single-threaded; a shrink yields the intended `RuntimeError`.
- **Not type confusion** — `PyObject_GetOptionalAttr` type-checks `name` at
  `Objects/object.c:1396`; a planted non-`str` yields a clean `TypeError`.
- **It is silent data loss** — rewriting `["a","b","c"]` → `["a","b","b"]` from `__getattr__` makes
  `pickle.dumps`/`loads` **succeed** while dropping slot `c`: `state = (None, {'b': 'B-value'})`,
  round-tripped `c` = missing. Identical on `debug-gil-nojit` and `debug-ft-nojit`.

### [CONSIDER] `reduce_newobj:8230/8233/8264/8280` — the four `Py_TYPE(obj)` reads do disagree

The brief's item 6. **Confirmed: they disagree and `pickle.dumps` silently emits an unloadable
pickle** — but the segfault the `:8318-8332` comment blames stays fixed.

Two user-code windows sit between read #1 and reads #2/#3: `_PyObject_GetNewArguments` (`:8236`,
`__getnewargs_ex__`/`__getnewargs__`) and `import_copyreg` (`:8239`, `PyImport_Import` →
`__import__`); `object_getstate` (`:8296`) opens a third. End-to-end symptom:

```
pickle.dumps succeeded, len = 32
loads FAILED: TypeError : Bnew.__new__() takes 1 positional argument but 4 were given
```

`dumps` pairs class A's `__getnewargs__` payload with class B's name and reports success; the
corruption surfaces only at load time. The copyreg window needs **no `__getnewargs__` at all** and
is genuinely reachable — `copyreg` is imported lazily, so the first protocol-2 reduce in a process
passes through `PyImport_Import`.

**Both escalation routes were checked and are closed**: bypassing the `tp_new == NULL` guard at
`:8230` is unreachable (every `tp_new == NULL` type found is `Py_TPFLAGS_IMMUTABLETYPE`, rejected by
`object_set_class_world_stopped:7753-7761`), and flipping `PyList_Check`/`PyDict_Check` between
`:8296` and `:8302` is rejected by the layout check at `:7688` / the immutability check at `:7757`.
Neither read dereferences stale memory. 4 hooks × 600 iterations × 6 protocols, clean under
`debug-gil-nojit-asan`.

**Guarded twins, both in this file.** `object_repr:7487` snapshots `type = Py_TYPE(self)` **once**
and reuses it at `:7488`/`:7494`/`:7503` across `type_module()` and `type_qualname()`, both of which
run metaclass code. `object_set_class:7825` does the same with `oldto`. `reduce_newobj` is the
outlier that re-reads. Fix: hoist one `PyTypeObject *cls = Py_TYPE(obj);` above `:8230`.

### [CONSIDER] `object_set_class_world_stopped:7757`, `:7635`, `:7688` — three error paths raise under STW

`object_set_class:7823` stops the world whenever `self` is not uniquely referenced — the common
case — and all three `PyErr_Format` rejections then run inside that region. `PyErr_Format`
allocates, formats, and under implicit chaining can enter `_PyObject_CallOneArg`.

Three guarded twins: `Python/gc_free_threading.c:2111`/`:2121` call `_PyEval_StartTheWorld(interp)`
**immediately before** `PyErr_NoMemory()`; `Objects/typeobject.c:6509-6513` states the rule outright;
and **this very function pair already defers `Py_DECREF(oldto)` to `:7832`, after
`types_start_world()`** — so the deferral discipline exists here and the raising was simply not given
it. **Not reproduced** (8 threads × 2000 failing assignments on `debug-ft-nojit`: no deadlock,
abort, or unexpected exception), so CONSIDER on the twins, not on a repro. Formally
`stw-safety-checker` territory; recorded here because the offending calls are all on error paths.

### Clean negatives in R25/R26, with denominators

- **Partial-state-on-error — R25 is CLEAN.** Every fallible step (`compatible_for_assignment:7763`,
  `_PyObject_MaterializeManagedDict_LockHeld:7769`, `_PyDict_DetachFromObject:7779`) completes
  *before* `Py_SET_TYPE(self, newto)` at `:7790`, and **nothing fallible follows the mutation** —
  `:7790` → `:7792 return 0`. The one committed state change on a failure path (a materialized
  managed dict left behind when `_PyDict_DetachFromObject` fails) is semantically transparent to
  Python and `detach_dict_from_object` (`Objects/dictobject.c:7976-7982`) commits `ma_values` and
  invalidates the inline values as its last two steps, so there is no torn intermediate. Verified
  live: after a rejected `x.__class__ = int`, `x` is still `X` with its `__dict__` intact.
- **Sentinel with no exception — CLEAN, 53/53.** All three sentinel-returning-lookup sites
  (`_PyObject_LookupSpecial` at `:8101`/`:8152`, `PyImport_GetModule` at `:7863`) pair with a
  correct `else if (PyErr_Occurred())` at `:8146`/`:8168`/`:7867`.
- **Success with a live exception — CLEAN.** The one lookalike (`:7995`,
  `if (value == NULL) { /* not an error */ }`) is correct by `PyObject_GetOptionalAttr`'s contract.
- **Discarded fallible int status — CLEAN, zero instances** in both regions. A mechanical
  bare-expression sweep returned 6 hits (`:7811`, `:7931`, `:7970`, `:8131`, `:8140`, `:8233`) and
  all six are continuation lines of multi-line `PyErr_Format(...)` calls, not statements. **This is
  a concrete FP class for the §6 `discarded_int_status` proposal** — the rule must key on AST
  `expression_statement` nodes, not on a line-oriented regex, or it will fire on every wrapped
  argument list.

---

## 5. Clean negatives inherited from pass 1 — re-confirmed, not re-read

- `alloc_null_no_memerror`: 0 findings over a hand-verified denominator of **7** raw allocation
  sites (pass 1 §4). Scanner still reports 0. Earned zero.
- `missing_null_check` / `unchecked_return` / `unchecked_parse` / `pylong_sentinel_no_errcheck`: 0
  over `fallible_assignment_sites=45`.
- The remaining five `PyErr_Clear` sites (`:3023`, `:8619`, `:9786`, `:10931`, `:11032`) — pass 1
  accounted for all eleven. Unchanged.

---

## 6. TOOLKIT ASSESSMENT

### Precision per rule (this slice, pass 2)

| rule | candidates | FIX | CONSIDER | ACCEPTABLE | net new | precision |
|---|---|---|---|---|---|---|
| `unconditional_pyerr_clear` | 5 | 4 (recorded: CPY-0076 ×2, CPY-0077 ×2) | 0 | 1 | **0** | 80% (unchanged from pass 1) |
| `int_status_never_tested` | 1 | 1 (recorded: CPY-0070) | 0 | 0 | 0 | **100%** |
| `alloc_null_no_memerror` | 0 (denom 7) | — | — | — | — | earned zero |
| `missing_null_check`, `unchecked_return`, `unchecked_parse`, `pylong_sentinel_no_errcheck` | 0 (denom 45) | — | — | — | — | earned zero |
| **by reading** | — | **6** | **9** | — | **15** | — |

Every scanner finding this pass was already recorded. **All fifteen new findings came from
reading**, and five of them (`:6744`, `:12177`, `:1111`, `:1990`, plus `:11090` recovered from pass
1) are instances of a shape the shipped rules *nearly* cover. That is the actionable signal.

The two hardest crashes of the pass — the `set_mro_error:3320` heap-use-after-free and the
`object_getstate_default:7990` SIGSEGV — are **not** error-path shapes at all. They belong to
`refcount-auditor` (borrowed-ref-across-call) and `ft-race-scanner`. They surfaced here because the
regions were read end to end for error-return semantics and the borrowed loads were on the error
paths. Both are handed off in §6 with their discriminators.

### The `_FALLIBLE_REGION_RE` gate is earning its keep — measured

I re-ran `_check_int_status_never_tested` with the gate neutralised: **7 candidates instead of 1**.
The 6 suppressed are `:1964` (the same site, duplicate offset), `:2030 type_set_bases`,
`:4491 type_new_set_module`, `:6833 type_setattro`, `:9627 PyType_Ready`, `:9717 add_subclass`,
`:10514 add_tp_new_wrapper`. I read all six: every one is the accumulate-then-return idiom
(`res = f(); <cleanup only>; return res;`). **Gate precision on this file: 6/6.** Keep it.

### RECALL GAP A — the rule cannot see a discarded status (shape)

`_INT_ASSIGN_TEMPLATE` requires `var = callee(`. A bare `callee(args);` expression statement —
the status discarded outright, which is *strictly worse* than assigned-and-untested — is invisible.
**Three of my four FIX findings live in that shape** (`:6744`, `:12177`, `:1111`).

### RECALL GAP B — the callee population excludes 0/1-valued status functions

`int_status_callees()` admits a same-file `int` helper only if `'-1' in func["body"]`. Measured on
this file: **154 `int`-returning functions, 104 admitted, 49 excluded.** `assign_version_tag` is
excluded — its documented convention is *"Return 0 if this cannot be done, 1 if `tp_version_tag` is
set"* (`:1414`), so the literal `-1` never appears. Finding `:1111` is invisible for this reason
*in addition to* gap A.

**Proposal — a new rule `discarded_int_status`.** Candidate = an `expression_statement` whose sole
child is a `call_expression` with an identifier callee, where the callee is in `INT_STATUS_APIS`
**or** is a same-file function whose return type is `int` (drop the `'-1' in body` filter; add
`|| '\breturn 0' in body` at minimum). Measured on `Objects/typeobject.c`:

| line | site | verdict |
|---|---|---|
| `:1111` | `PyType_Watch` → `assign_version_tag` | **FIX (net new)** |
| `:6359` | `_PyType_LookupStackRefAndVersion` → `assign_version_tag` | ACCEPTABLE |
| `:6744` | `update_slot_after_setattr` → `update_slot` | **FIX (net new)** |
| `:10024` | `wrap_sq_item` → `check_num_args` | ACCEPTABLE |
| `:11090` | `has_dunder_getitem` → `lookup_maybe_method` | **FIX — recovers CPY-0075** |
| `:12136` | `fixup_slot_dispatchers` → `update_one_slot` | ACCEPTABLE |
| `:12177` | `update_all_slots` → `update_slot` | **FIX (net new)** |

**7 candidates, 4 FIX, 57% precision — and it independently recovers pass 1's T2 exemplar
(`has_dunder_getitem:11090` = CPY-0075) plus three net-new bugs.** T2 was proposed in pass 1 and
not shipped; this is the calibration data for shipping it.

### Two suppressions the new rule needs on day one

1. **"Called only for its raising side effect."** `wrap_sq_item:10024` is a discarded call
   immediately followed by `assert(PyErr_Occurred()); return NULL;`. Suppress when the discarded
   call is followed within ~3 lines by a sentinel return *and* an `assert(PyErr_Occurred())` or a
   `PyErr_*` reference. **This also defeats pass 1's naive in-file-disagreement heuristic**:
   `check_num_args` has 17 call sites, 16 test the result and 1 does not — and the 1 is correct.
   Disagreement alone is not sufficient.
2. **"Status recovered from a field."** `:6359` discards `assign_version_tag` but re-reads
   `type->tp_version_tag` on the next line and tests it. Suppress when a field the callee writes is
   read and tested within the following few statements.

### RECALL GAP C — preprocessor-arm error-handling asymmetry (new rule, very high precision)

The sharpest mechanical shape found this pass:

> the **same callee** is called in both arms of a `#ifdef Py_GIL_DISABLED` / `#else` pair, its
> result **tested in one arm and discarded in the other**.

Two instances in this file (`:6734` vs `:6744`; `:12152` vs `:12177`) and **both are true
positives** — the tested arm is literally the fix for the untested one, so the rule ships its own
guarded twin. Cheap to implement (the scrubbed-macro pass already knows the conditional groups) and
it generalises to every `#ifdef`/`#else` pair, not just `Py_GIL_DISABLED`. Note the FP taxonomy's
existing entry *"Never reason across a `#if`/`#else`/`#endif` boundary"* is about **dataflow**;
this rule reasons about **treatment symmetry**, which is the opposite and is sound.

### RECALL GAP D — a documented postcondition whose establishing call is discarded

`PyType_Watch:1110` reads `// ensure we will get a callback on the next modification` immediately
above the discarded `assign_version_tag(interp, type);`. The comment *is* the specification and the
discarded call is the only thing implementing it. A cheap heuristic: a discarded fallible call whose
immediately preceding comment contains `ensure|must|guarantee|so that` is a high-prior candidate.
Low recall by nature, but near-zero cost and it fired on a real bug here.

### RECALL GAP E — partial rollback across chained error labels

No rule models §2's third finding. The mechanical tell is precise:

> two error labels where the first falls through into the second, plus a `Py_DECREF`/`Py_CLEAR` of
> the rollback log on the **success** path between them, plus a later `goto <second label>`.

`type_set_bases_unlocked` has exactly this: `undo:` (`:1984`) falls into `bail:` (`:2000`),
`Py_DECREF(temp)` sits at `:1952`, and `:1968` jumps to `bail:`. That is statically decidable and
would have flagged a reproduced FIX. Worth a slice to calibrate on `Objects/` generally — the
goto-ladder idiom is everywhere in CPython, so the precision is unknown, but the *shape* (a
rollback log freed before the last failure exit) is narrow enough to be worth trying.

### The same two gaps sink the shipped `unchecked_parse` rule — and it is a 7-vs-1 in-file split

`_check_unchecked_parse` (`scan_error_paths.py:286`) keys on
`(\w+)\s*=\s*PyArg_Parse(?:Tuple|TupleAndKeywords)\s*\(`. It therefore misses
`type_set_bases_unlocked:1990` for **both** of the reasons above and one more:

1. it requires an **assignment** (gap A), and `:1990` is a bare statement;
2. its API set omits **`PyArg_UnpackTuple`** entirely (a narrower version of gap B).

Verified in-file: **8** `PyArg_UnpackTuple` call sites — `:1990`, `:10037`, `:10093`, `:10190`,
`:10300`, `:10321`, `:10350`, `:10371` — of which **7 are written `if (!PyArg_UnpackTuple(...))`
and exactly 1 is not.** That is the guarded-twin heuristic at its strongest, on a shipped rule that
reported `unchecked_parse_calls: 0` for this file. Adding `PyArg_UnpackTuple`/`PyArg_ParseStack*`
to the API set and accepting the bare-statement form is a two-line change.

**This unifies the recall story: both of `scan_error_paths.py`'s result-checking rules require
`var = callee(...)`, and both are blind to the strictly-worse bare-statement form.** One fix — walk
`expression_statement` nodes — serves both.

### RECALL GAP F — `PyList_GET_ITEM` on a *published* container (hand-off to refcount / ft-race)

Not an error-path rule, but it was found by reading in this lane and it produced the single
hardest crash of the pass, so it is recorded here for whichever agent owns it.

> `PyList_GET_ITEM` / `PyTuple_GET_ITEM` in a C loop over a container that is **reachable from
> Python**, across a call that can run user code or concurrently with another thread.

The discriminator is mechanical and cheap: is the container *private* (freshly constructed in this
function, or a local accumulator) or *published* (returned by an accessor that reads a type dict,
module state, or interpreter state)? In `Objects/typeobject.c` there are **4** `PyList_GET_ITEM`
sites (verified: `:1853`, `:1990`, `:6499`, `:7990`); three are private and one (`:7990`,
`cls.__slotnames__`) is published — and the published one is a reproduced SIGSEGV. That is a
1-in-4 hit rate on a one-predicate test.

**Correction to the sub-read.** It offered `Python/codecs.c:69` and `:183` as further unfixed
instances. They are not — I checked both: `codecs.c:69` reads
`PyObject *item = PyList_GetItemRef(codec_search_path, i);` **under
`FT_MUTEX_LOCK(&interp->codecs.search_path_mutex)`**, and `:183` reads
`func = PyList_GetItemRef(interp->codecs.search_path, i);` with a NULL check. They are the
**guarded twins** — the same shape over the same kind of published, user-mutable list, already
converted. That makes them stronger evidence for `:7990` than the sub-read claimed, not weaker:
CPython has already fixed this exact pattern elsewhere and `typeobject.c:7990` was missed.

### A third and fourth suppression the `discarded_int_status` rule needs

The R25/R26 sweep found **6** bare-expression "hits" in those regions and **all six** were
continuation lines of multi-line `PyErr_Format(...)` argument lists. The rule must key on
tree-sitter `expression_statement` nodes whose sole named child is a `call_expression` (as my
`int_status_sweep.py` does) and must never be implemented as a line-oriented regex, or it will fire
on every wrapped call in the tree.

Fourth: **resolve the callee's return type.** `type_mro_modified:1278` and
`_PyType_Modified_Unlocked` both return `void`, so their bare-statement call sites at `:3680`,
`:3683`, `:3687` are not discarded statuses at all. The population must come from the extracted
`return_type`, which it already does — just do not let a name-only match through.

### Confirmed still-open toolkit items from pass 1

- **T1** (`DKIX_ERROR` not recognised as a failure test) — still true; `:6183` is still unflagged by
  both scanners.
- **T3** (suppress the "early-return guard clause" class in the scanner) — still true; `:9748` still
  costs a candidate slot and is still named verbatim in the taxonomy.
- **T4** (the `type_module()` majority-vote template) — not implemented; would still fire correctly.

### Reproduction notes

All scripts are `.py` files (no heredocs) under
`/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/`.

| script | finding |
|---|---|
| `p2_update_slot_after_setattr.py` | §2a `update_slot_after_setattr:6744` |
| `p2_update_all_slots.py` | §2b `update_all_slots:12177` |
| `p2_bases_mro_desync.py` | §2c partial rollback at `bail:` |
| `p2_type_watch_silent.py`, `p2_type_watch_pure.py`, `p2_type_watch_custom_mro.py` | §2d `PyType_Watch:1111` (3 triggers) |
| `p2_setflags_sibling.py`, `p2_setflags_window.py` | §3 gh-148450 sibling |
| `p1c_asan.py`, `p1d_segv.py`, `p1b_proof.py` | §4a `set_mro_error:3320` UAF |
| `p2_mrocheck_gaps.py`, `p4_assert_mro0.py` | §4a `mro_check` / `mro[0]` assert |
| `p3_gc_window.py` | §4a the two unreproduced GC windows |
| `min_repro_7990.py`, `ft_slotnames_race.py` | §4b `object_getstate_default:7990` |
| `r25_r26_probe{,2,3}.py`, `sweep_r25r26.py` | §4b guard/`reduce_newobj` probes + denominators |
| `int_status_sweep.py`, `int_status_ungated.py`, `callee_discovery_gap.py` | §6 sweep tooling |

Every run named an explicit interpreter (ambient `python` is RustPython and silently emits nothing).
**Every crash claim relayed from a sub-read was re-run by me before it entered this report** —
`min_repro_7990.py` (3/4 SIGSEGV on `release-ft-nojit`, clean on `release-gil-nojit`),
`p1d_segv.py` (SIGSEGV on `debug-gil-nojit`), `p1c_asan.py` (ASan heap-use-after-free at
`typeobject.c:3320`, freed at `:3322`), `p4_assert_mro0.py` (SIGABRT at `typeobject.c:6053`).
One relayed claim was **wrong and is corrected in §6**: `Python/codecs.c:69`/`:183` were offered as
unfixed siblings of §4b's `PyList_GET_ITEM` bug; they already use `PyList_GetItemRef` and are the
guarded twins.
