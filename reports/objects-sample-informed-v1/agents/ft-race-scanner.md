# ft-race-scanner — Objects/ sample (informed)

Target: `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0, full clone).
Reproduction platforms used (both free-threaded, both in-tree):

| build | version | flavour | used for |
|---|---|---|---|
| `/home/danzin/projects/ft_cpython` | 3.16.0a0 @ `a7885b46f15` | FT + ASan + debug refcounts | T3 crash repros |
| `/home/danzin/projects/3.14_tsan_debug_ft` | 3.14.3+ | FT + TSan + debug | T2 data-race confirmation |

Note: `/home/danzin/projects/cpython/python` is **not** a free-threaded build
(`config_read_gil: Disabling the GIL is not supported by this build`) — the two builds above were
used instead. All flagged source lines are byte-identical between HEAD and both repro trees.

## Scanner volume

raw candidates in sample: 6 | confirmed: 3 | dismissed: 3 | **precision: 50%**

Per tier: **T2 2/3 (67%)** · **T3 1/1 (100%)** · **T1 0/2 (0%)**.
Plus **3 findings the scanner did not surface** (2 of them reproduced as live crashes).

---

## Findings

### FIX

#### 1. [FIX] `ga_getitem` defeats last week's gh-153298 critical section — `Objects/genericaliasobject.c:583`

**What.** `ga_getitem` (the `mp_subscript` slot — `list[T][int]`) lazily populates
`alias->parameters` inline, with no critical section:

```c
static PyObject *
ga_getitem(PyObject *self, PyObject *item)          /* :579 */
{
    gaobject *alias = (gaobject *)self;
    // Populate __parameters__ if needed.
    if (alias->parameters == NULL) {                /* :583 */
        alias->parameters = _Py_make_parameters(alias->args);
```

**Guarded twin — two of them, and the fresher one is 7 days old.**
- `ga_parameters` / `ga_parameters_lock_held`, **same file, same field**, `Objects/genericaliasobject.c:844-865`
  — wrapped in `Py_BEGIN_CRITICAL_SECTION(self)` by **`68abf17fa92` (gh-153298, 2026-07-08)**.
- `union_init_parameters`, `Objects/unionobject.c:327`, the *sibling type's* version of the identical
  lazy init — guarded by **`dc3e9638c22` (gh-132713, 2025-04-23)**, and correctly called from **both**
  `union_getitem:345` and `union_parameters:367`.

**Why this is a bug and not a duplicate of gh-153298.** gh-132713 fixed unionobject by routing
*every* accessor through one guarded helper. gh-153298 fixed genericalias by guarding *one* accessor
and leaving the other alone. **A critical section held by only one of two accessors serializes
nothing** — `alias.__parameters__` (guarded) races `alias[int]` (unguarded) on the same field, and
`alias[int]` races itself.

**Reproduced (TSan, `3.14_tsan_debug_ft`, `t2_repro.py ga`)** — three distinct race reports:
```
data race  genericaliasobject.c:578:27 in ga_getitem   <- write x write, ga_getitem vs ga_getitem
data race  genericaliasobject.c:577:16 in ga_getitem   <- read  x write, ga_getitem vs ga_parameters:842
data race  genericaliasobject.c:415:27 in _Py_subs_parameters
             (reads alias->parameters while another thread's _Py_make_parameters:218 is still
              filling the tuple it will publish)
```
(3.14 line 577/578 == HEAD line 583/584. On 3.14 `ga_parameters` is also unguarded; on HEAD it is
guarded, which changes nothing for the `ga_getitem`-vs-`ga_getitem` race and leaves the cross race
intact because only one side takes the lock.)

**Impact.** Both threads compute a parameters tuple; the loser's tuple is overwritten by a plain
assignment with no `Py_DECREF` of the previous value → permanent leak per lost race, plus an
unsynchronised pointer publication that `_Py_subs_parameters` immediately dereferences.

**Python-level reach.** `typing.TypeVar`-parameterised aliases subscripted from more than one thread:
`t = list[T]` in a shared slot, `t[int]` in thread A, `t.__parameters__` in thread B. This is exactly
the workload gh-153298's own regression test (`Lib/test/test_free_threading/test_types.py`) models —
the test only exercises `__parameters__`, which is why the sibling survived.

**Fix.** Route `ga_getitem` through `ga_parameters_lock_held` the way `union_getitem` routes through
`union_init_parameters`, and take a reference to `alias->parameters` inside the section rather than
re-reading the field for the `_Py_subs_parameters` call.

---

#### 2. [FIX] Generic sequence-iterator double-DECREF — `Objects/iterobject.c:79-80` (`iter_iternext`)

**What.** The `iter(obj)` fallback iterator (`PySeqIter_Type`, used for any object with
`__getitem__` and no `__iter__`) drops its owning reference on exhaustion with nothing serialising it:

```c
    seq = it->it_seq;                                   /* :60  borrowed load */
    ...
    result = PySequence_GetItem(seq, it->it_index);      /* :69  runs arbitrary Python */
    ...
        PyErr_Clear();
        it->it_seq = NULL;                               /* :79 */
        Py_DECREF(seq);                                  /* :80 */
```

**Guarded twins (both strategies present in the sample).**
- Critical section: `odictiter_iternext`, `Objects/odictobject.c:1866` —
  `Py_BEGIN_CRITICAL_SECTION(op); res = odictiter_iternext_lock_held(op); Py_END_CRITICAL_SECTION();`
- Elide the drop under FT: `tupleiter_next`, `Objects/tupleobject.c:1146-1150` —
  `#ifndef Py_GIL_DISABLED / it->it_seq = NULL; Py_DECREF(seq); / #endif`.
  Same treatment in `listobject.c:4080`, `enumobject.c:458`.

**Reproduced — hard failure** (`ft_cpython`, `PYTHON_GIL=0`, `t3_repro.py seqiter`):
```
Include/internal/pycore_stackref.h:726: _Py_NegativeRefcount: Assertion failed: object has negative ref count
object refcount : -2
object type name: S
Fatal Python error: _PyObject_AssertFailed
  UNDERFLOW: base=2 after=-2 (lost 4)
```
Four threads each dropped the iterator's single owned reference. On a release build this is a
premature free followed by use-after-free, not an assertion.

**Python-level reach.**
```python
class S:
    def __getitem__(self, i): raise IndexError
it = iter(S())          # -> PySeqIter_Type
# two threads: next(it)
```
Nothing prevents two threads calling `next()` on one iterator; the exhaustion branch is entered by
both because `PySequence_GetItem` runs Python code, which is a wide window.

**Fix.** Either wrap the body in `Py_BEGIN_CRITICAL_SECTION(iterator)` (odict strategy) or bracket
`:79-80` in `#ifndef Py_GIL_DISABLED` (tuple/list/reversed strategy — a two-line change already
applied to every other builtin sequence iterator).

---

#### 3. [FIX] `ga_iternext` NULL-deref SIGSEGV — `Objects/genericaliasobject.c:952` — **scanner missed this**

**What.**
```c
static PyObject *
ga_iternext(PyObject *op)                       /* :939 */
{
    gaiterobject *gi = (gaiterobject*)op;
    if (gi->obj == NULL) { ... return NULL; }   /* :941  check */
    gaobject *alias = (gaobject *)gi->obj;      /* :945  borrowed deref */
    PyObject *starred_alias = Py_GenericAlias(alias->origin, alias->args);
    ...
    Py_SETREF(gi->obj, NULL);                   /* :952  drop */
```
`Py_SETREF(x, NULL)` expands to `_tmp = x; x = NULL; Py_DECREF(_tmp)` — and unlike `Py_CLEAR` it has
**no NULL guard**. Thread B can pass the `:941` check, thread A can complete the `:952` store, and
thread B then evaluates `Py_DECREF(NULL)`.

**Reproduced — SIGSEGV** (`ft_cpython` ASan/FT, `t3_repro.py gaiter`):
```
AddressSanitizer: SEGV on unknown address 0x00000000000c ... READ memory access
    #0 _Py_atomic_load_uint32_relaxed  Include/cpython/pyatomic_gcc.h:367
    #1 Py_DECREF                        Include/refcount.h:345
    #2 ga_iternext                      Objects/genericaliasobject.c:942   (== HEAD :952)
    #3 builtin_next                     Python/bltinmodule.c:1770
```

**Python-level reach.** `it = iter(list[int])`, then `next(it)` from two threads. One line of Python.

**Guarded twin.** `odictiter_iternext` (`odictobject.c:1866`). Also note the second, weaker
mitigation available for free: switching `Py_SETREF(gi->obj, NULL)` to `Py_CLEAR(gi->obj)` downgrades
the SIGSEGV to a double-DECREF; the critical section is the actual fix.

---

#### 4. [FIX] `calliter_iternext` double-DECREF — `Objects/iterobject.c:243-244` and `:249-250`

**What.** `iter(callable, sentinel)`. Both exhaustion branches drop both owning members with no
serialisation:
```c
        if (ok > 0) {
            Py_CLEAR(it->it_callable);      /* :243 */
            Py_CLEAR(it->it_sentinel);      /* :244 */
        }
    }
    else if (PyErr_ExceptionMatches(PyExc_StopIteration)) {
        PyErr_Clear();
        Py_CLEAR(it->it_callable);          /* :249 */
        Py_CLEAR(it->it_sentinel);          /* :250 */
```
Additionally `_PyObject_CallNoArgs(it->it_callable)` at `:233` uses a **plain read of the member** as
a borrowed callee across arbitrary Python — a concurrent exhaustion in another thread frees it.

**Status: static only.** 400 rounds × 4 barrier-synchronised threads did **not** trigger it
(`t3_repro.py calliter`, 0/400). `Py_CLEAR`'s internal NULL check means the double-DECREF requires
*both* threads to load the same non-NULL pointer before *either* stores — a much tighter window than
finding #3's `Py_SETREF`. The shape is nonetheless identical to gh-154130/gh-144357 and the same
one-line critical section fixes it. Recorded as FIX with reproduction pending a targeted TSan run.

**Guarded twin.** `odictiter_iternext` (`odictobject.c:1866`).

---

#### 5. [FIX] TSAN-0043 confirmed still present — `Objects/descrobject.c:624` (`descr_get_qualname`)

One line per the informed-mode rule. Unchanged at HEAD; `Objects/descrobject.c` contains **zero**
`Py_BEGIN_CRITICAL_SECTION` and zero atomics. Re-confirmed live under TSan
(`t2_descr.py`, `3.14_tsan_debug_ft`):
`data race descrobject.c:624:16 in descr_get_qualname` and `:625:27` (the read and the store).
Its guarded twin is now `ga_parameters_lock_held` (`genericaliasobject.c:844`, gh-153298, 7 days
before HEAD) — the identical `if (x->f == NULL) x->f = compute();` shape, wrapped.
Reachability note worth adding to the catalog entry: `descr_new` calls
`_PyObject_SetDeferredRefcount` (`descrobject.c:913`), i.e. descriptors are explicitly
process-shared long-lived objects, and `d_qualname` is their only mutable field. The race window is
the **first** concurrent `__qualname__` read per descriptor — narrow per object, but there are
thousands of C descriptors and `inspect`/`pydoc` in a thread pool walks them all.

---

### CONSIDER

#### 6. [CONSIDER] `PyFunction_GetAnnotations` reaches a de-facto lock-held helper without the lock — `Objects/funcobject.c:581` → `:534`

`func_get_annotation_dict` (`:534`) is a materialise-once helper with **two** stores to
`op->func_annotations`:
- `:552` `Py_XSETREF(op->func_annotations, ann_dict)` after calling `__annotate__` (arbitrary Python)
- `:574` `Py_SETREF(op->func_annotations, ann_dict)` on the tuple→dict conversion

It has two callers:
- `function___annotations___get_impl` (`:881`, call at `:891`) — **guarded**: Argument Clinic
  `@critical_section` emits `Py_BEGIN_CRITICAL_SECTION(self)` in
  `Objects/clinic/funcobject.c.h:94`. **This is the guarded twin.**
- `PyFunction_GetAnnotations` (`:581`) — **unguarded** public `PyAPI_FUNC`
  (`Include/cpython/funcobject.h:82`).

Two threads through the unguarded path both hit `:574`, both `Py_SETREF` the same annotate tuple →
double DECREF → refcount underflow. The function also returns a **borrowed** reference to a field
another thread may `Py_SETREF` out from under the caller.

**CONSIDER, not FIX**: the only in-tree caller of `PyFunction_GetAnnotations` is
`Modules/_testcapi/function.c:129`, so this is a latent C-API hazard for extensions rather than a
Python-reachable bug today. It is also the inverse of the `*_lock_held` convention: a function that
*is* lock-held by contract but is not *named* so, therefore nothing flags the unguarded caller.

**Fix.** Rename to `func_get_annotation_dict_lock_held` and give `PyFunction_GetAnnotations` the
critical section (matching the clinic getter).

#### 7. [CONSIDER] `templateiter_next` interleaving race — `Objects/templateobject.c:19`

`self->from_strings` is a plain `int` read-modify-written across two `PyIter_Next` calls (`:24`,
`:30`, `:35`), and both sub-iterators are used as plain-loaded borrowed pointers across those calls.
Two threads iterating one `Template` interleave the strings/interpolations alternation, producing
duplicated or skipped items. No owning-ref drop, so **not** a T3 — this is data corruption, not
memory unsafety. `t"..."` templates are cheap to share across threads. Fix: `Py_BEGIN_CRITICAL_SECTION(op)`.

---

### POLICY / ACCEPTABLE (one line each)

- **POLICY** `Objects/capsule.c:161,174,187,200,223` — `PyCapsule_SetPointer/SetName/SetDestructor/SetContext/SetTraverse` do plain writes to a shared `PyObject` and the getters do plain reads, with no documented FT thread-safety contract either way. Not a code bug; a docs gap that should be resolved explicitly ("capsule mutation is the caller's responsibility") rather than left ambiguous.
- **ACCEPTABLE (FP)** `Objects/unionobject.c:171` `unionbuilder_add_single_unchecked` — `ub` is a **stack-local aggregate** (`unionbuilder ub;` at `:264`, `:400`, `:487`), never shared. Not lazy init of shared state.
- **ACCEPTABLE (FP)** `Objects/tupleobject.c:1165` `it_index` — the plain access is inside the `#else` arm of `#ifdef Py_GIL_DISABLED` (`tupleiter_len:1158-1168`); the FT arm uses `FT_ATOMIC_LOAD_SSIZE_RELAXED`. Textbook `#ifdef`-split FP from the agent definition.
- **ACCEPTABLE (FP)** `Objects/weakrefobject.c:65` `wr_object` — `init_weakref` plain write before the weakref is published; every post-publication access uses `FT_ATOMIC_STORE_PTR` (`:89`) or runs under the striped lock. Init-time-write FP.
- **ACCEPTABLE** `Objects/odictobject.c:1757` (`di->di_size` staleness sentinel) and `:1951` (`di_result` local) — matched my sweep's lazy-init regex but are not lazy init; both already run under `Py_BEGIN_CRITICAL_SECTION`.

---

## New siblings of known shapes

**The fix-propagation yield.** Two independent multi-year sweeps left siblings behind; both are
visible from inside this 14-file sample.

**Sweep A — T2 lazy-init `parameters`** (`gh-132713` 2025-04-23 → `gh-153298` 2026-07-08):

| site | accessor | guarded? |
|---|---|---|
| `unionobject.c:327` `union_init_parameters` | both `union_getitem` and `union_parameters` | ✅ gh-132713 |
| `genericaliasobject.c:844` `ga_parameters_lock_held` | `__parameters__` getset | ✅ gh-153298 |
| **`genericaliasobject.c:583` `ga_getitem`** | **`alias[...]` mp_subscript** | ❌ **left behind** |
| **`descrobject.c:624` `descr_get_qualname`** | **`__qualname__` getset** | ❌ **TSAN-0043** |

**Sweep B — T3 iterator exhaustion drop** (gh-112087 2024-02 listiter → gh-120608 2024-06 reversed →
gh-115999 2025-01 list+tuple → gh-144357 set → gh-154130 dict → gh-153296 StringIO):

| `tp_iternext` | drop spelling | status |
|---|---|---|
| `odictobject.c:1866` | `Py_CLEAR` under CS | ✅ critical-section strategy |
| `tupleobject.c:1147` / `listobject.c:4080` / `enumobject.c:458` | `#ifndef Py_GIL_DISABLED` | ✅ elide-drop strategy |
| `dictobject.c:6181` | CS + lock_held | ✅ (fast path still TSAN-0053) |
| **`iterobject.c:79` `iter_iternext`** | `=NULL; Py_DECREF` | ❌ **in sample — reproduced, refcnt -2** |
| **`iterobject.c:243/249` `calliter_iternext`** | `Py_CLEAR` ×2 | ❌ **in sample** |
| **`genericaliasobject.c:952` `ga_iternext`** | `Py_SETREF(…, NULL)` | ❌ **in sample — reproduced, SIGSEGV** |
| `bytesobject.c:3451` `striter_next` | `=NULL; Py_DECREF` | ❌ scope escape |
| `unicodeobject.c:14963` `unicodeiter_next` | `=NULL; Py_DECREF` | ❌ scope escape |
| `unicodeobject.c:14986` `unicode_ascii_iter_next` | `=NULL; Py_DECREF` | ❌ scope escape (scanner did flag) |
| `listobject.c:4238` `listreviter_next` | `=NULL; Py_DECREF` | ❌ scope escape |
| `memoryobject.c:3639` `memoryiter_next` | `=NULL; Py_DECREF` | ❌ scope escape (scanner did flag) |

The pattern in Sweep B is unmistakable: **the sweep followed the concrete container types and never
reached the generic/protocol iterators** — `iter(seq)`, `iter(c, sentinel)`, and the generic-alias
unpack iterator are precisely the ones that are *not* a named container.

---

## Classes bounded (clean negatives)

- **`Objects/cellobject.c` — clean, and contradicts the run brief's hypothesis.** `cell_contents` is
  writable, but every `ob_ref` access funnels through `PyCell_GetRef` / `PyCell_SetTakeRef` /
  `PyCell_SwapTakeRef` in `Include/internal/pycore_cell.h:19-49`, all three of which take
  `Py_BEGIN_CRITICAL_SECTION(cell)` (and the FT arm additionally uses `_Py_XNewRefWithLock` /
  `FT_ATOMIC_STORE_PTR_RELEASE`). `cell_get_contents:147` and `cell_set_contents:158` both go
  through them. The only plain write is `op->ob_ref = Py_XNewRef(obj)` in `PyCell_New:19`,
  pre-publication. Scanner correctly returned 0 here.
- **`Objects/funcobject.c` function versioning — clean, also contradicts the brief.** Every
  `func_version` write is either pre-publication (`:147`, `:225`, and `_PyFunction_SetVersion:325`
  which is MAKE_FUNCTION-only and documents that no code is specialised on it yet) or under
  `_PyEval_StopTheWorld` (`_PyFunction_ClearVersion:355-364`). The entire `func_version_cache` is
  `#ifndef Py_GIL_DISABLED` (`:309-315`, `:326-330`, `:340-345`, `:369-378`). The plain read in
  `_PyFunction_GetVersionForCurrentState:384` cannot be concurrent with an STW writer.
  The *annotations* cache in the same file is the real issue — see finding 6.
- **`Objects/weakrefobject.c` `*_lock_held` convention — holds.** All five `clear_weakref_lock_held`
  call sites are covered: `:115` and `:1040`/`:1069` under `LOCK_WEAKREFS`, `:137`
  (`_PyWeakref_ClearRef`) via its own callers, `:165` (`gc_clear`) with an explicit STW comment.
  `_PyWeakref_ClearRef`'s callers are `_PyWeakref_ClearWeakRefsNoCallbacks:1125` (call at `:1134`,
  holds `LOCK_WEAKREFS`), `_PyStaticType_ClearWeakRefs:1113` (call at `:1120`, documented
  single-threaded finalization),
  and `Python/gc.c` / `Python/gc_free_threading.c` (GIL / STW, documented in the file header at
  `:30-32`). `weakref_hash_lock_held:190` ← `weakref_hash:205` under CS. **No violation found** —
  a clean negative for the calibration probe in the brief.
  One POLICY nit: `_PyWeakref_ClearRef` is exported as `PyAPI_FUNC` in
  `Include/cpython/weakrefobject.h:43` and calls a lock-held helper with no lock; an external caller
  cannot know the obligation.
- **`Objects/interpolationobject.c`, `Objects/lazyimportobject.c`, `Objects/structseq.c` — clean.**
  All members written only pre-publication and cleared only in `tp_clear`; no lazy caches, no
  `tp_iternext`, no atomics to be asymmetric with. `lazyimportobject.c` has zero mutable
  post-construction state; the actual resolution happens in `Python/import.c`
  (`_PyImport_LoadLazyImportTstate`) — **scope escape**, not covered here.
- **T1 in the sample is empty.** Both candidates are FP classes named in the agent definition. Only
  two sample files contain atomics at all (`tupleobject.c` 7, `weakrefobject.c` 3), and in both the
  atomic/plain split is deliberate.

---

## Toolkit assessment

### Precision

| tier | candidates | true | false | FP class |
|---|---|---|---|---|
| T3 `iternext_double_decref` | 1 (covering 2 sites) | 1 | 0 | — |
| T2 `lazy_init_no_critical_section` | 3 | 2 | 1 | stack-local aggregate (`unionbuilder ub;`) |
| T1 `atomic_plain_asymmetry` | 2 | 0 | 2 | `#ifdef Py_GIL_DISABLED` split; init-time plain write |
| **total** | **6** | **3** | **3** | **50%** |

T3's stated "high confidence" is earned — 1/1, and it pointed at a site I reproduced as a fatal
refcount underflow. T1's "low confidence" is also honest, but at 0/2 in this sample it is currently
pure noise; every one of its FPs is mechanically detectable (see proposal 3).

### Recall gaps

**R1 — `Py_SETREF(x->f, NULL)` is an unmodelled T3 drop spelling. This one hid a live SIGSEGV.**
`ga_iternext` (`genericaliasobject.c:952`) drops its owning member via `Py_SETREF`, which the T3 rule
does not match (it models `Py_CLEAR(x->f)` and `x->f = NULL; Py_DECREF(local)`). Worse, `Py_SETREF`
is **strictly more dangerous** than `Py_CLEAR`: `Py_CLEAR` has an internal NULL guard, `Py_SETREF`
does not, so the failure mode escalates from double-DECREF to `Py_DECREF(NULL)` → SEGV. This is the
single highest-value gap in the run.

**R2 — T3 under-fires by roughly 3×.** A brace-matched sweep of every function registered as
`tp_iternext` across all of `Objects/` (script: `scratchpad/t3sweep.py`) finds 13 exhaustion-drop
sites; the scanner reported 3 findings covering 4 sites. After removing the 3 that are correctly
`#ifndef Py_GIL_DISABLED`-guarded (tuple/list/reversed), **6 genuine drops were missed**:
`genericaliasobject.c:952` (R1), `iterobject.c:243` was caught only as a *duplicate* of `:79` rather
than as its own type+site, `bytesobject.c:3452`, `unicodeobject.c:14962`, `listobject.c:4238`,
`enumobject.c` reversed. Reporting a second distinct function as `duplicate_locations` rather than a
finding also under-counts the class in the summary block.

**R3 — T2's condition regex is single-line only.** `funcobject.c:885` reads
```c
    if (self->func_annotations == NULL &&
        (self->func_annotate == NULL || !PyCallable_Check(self->func_annotate))) {
        self->func_annotations = PyDict_New();
```
and never matched. My own first-pass sweep had the same defect. Related: the second store in
`func_get_annotation_dict` (`:574`) is gated on `PyTuple_CheckExact(...)`, not on `== NULL`, so a
"materialise-once cache" can be missed even with a multi-line fix.

**R4 — no model of Argument Clinic `@critical_section`.** For clinic-generated getters/setters the
guard lives in `Objects/clinic/<file>.c.h`, not in `<file>.c`. `function___annotations___get_impl`
looks completely unguarded in the source it was scanned from. Today this direction happens to be
safe only because R3 masked it; the moment R3 is fixed, every clinic-guarded lazy init in
`funcobject.c` / `codeobject.c` / `listobject.c` becomes a false positive.

**R5 — no model of `#ifndef Py_GIL_DISABLED` regions.** This is not a minor detail: it is the
**second canonical fix strategy for T3** (used by tupleiter, listiter, reversed) *and* the dominant
T1 FP source. A scanner that cannot see it can neither suppress the FPs nor recognise a fixed site.

**R6 — `*_lock_held` handling is suppression-only, never a check.** The convention is honoured
everywhere in the sample (verified for all 16 `_lock_held` sites across odict/weakref/genericalias),
so the calibration probe came back negative — but the *inverse* case is real and unmodelled:
`func_get_annotation_dict` is lock-held by contract, is **not named** `_lock_held`, and has one
guarded caller and one unguarded public-API caller (finding 6). The naming convention is exactly
what a scanner needs, and its absence is itself the smell.

### Prompt issues

1. The agent definition lists the T1 FP classes correctly but does not instruct me to check them
   *first*. Both T1 candidates were resolvable in under a minute by looking at the enclosing
   `#ifdef` / whether the write is in a constructor. Recommend an explicit early-exit:
   *"Before reading a T1 candidate's semantics, check (a) is the plain access inside
   `#ifndef Py_GIL_DISABLED`? (b) is it in a constructor before publication? If either, dismiss."*
2. "**The guarded twin is the fix**" (Important Guidelines) is right but names only one fix shape.
   Add the second: *"or a sibling that elides the drop entirely under `#ifdef Py_GIL_DISABLED` —
   for T3 this is the more commonly applied fix in Objects/."*
3. The definition never states the rule that produced the run's best finding:
   **a critical section held by only some accessors of a field protects nothing.** Recommend adding
   to the Analysis Strategy: *"When you find a guarded twin, immediately enumerate every other
   accessor of the same field. A field with one guarded and one unguarded accessor is a higher-
   confidence finding than a field with no guard at all, because the guard is proof the maintainers
   already agreed the field needs protection."*
4. Phase 2 says "Build CPython `--disable-gil` with ThreadSanitizer" as if from scratch. In this
   environment five prebuilt FT variants (including ASan and TSan) already exist under
   `/home/danzin/projects/`. Recommend the prompt tell the agent to look for an existing FT build
   before proposing a build — it turned a static report into two reproduced crashes.

### Concrete tuning proposals (ranked by value)

1. **Add `Py_SETREF` / `Py_XSETREF` to the T3 drop pattern.**
   Regex: `Py_X?SETREF\s*\(\s*\w+\s*->\s*\w+\s*,\s*NULL\s*\)`.
   Emit as a distinct `type` — `iternext_setref_null_decref`, `confidence: high` — with detail text
   noting that unlike `Py_CLEAR` this macro has no NULL guard, so the failure mode is
   `Py_DECREF(NULL)` → SIGSEGV rather than a double-DECREF. *Evidence: this single regex would have
   surfaced a reproduced segfault (`genericaliasobject.c:952`).*

2. **Add a `lazy_init_partial_guard` finding type — and yes, promote the lazy-init-cache pattern,
   but in this form.** The brief asks whether the lazy-init-cache pattern deserves a dedicated
   `type` now that gh-153298 supplies a canonical guarded twin. It does — but a bare
   `if (x->f == NULL) x->f = compute();` should **stay medium**, because in isolation it is often a
   single-threaded init path. The discriminator that earns FIX is the **twin**:

   > When a file contains ≥2 accessors of the same `X->field` in the lazy-init shape, and at least
   > one is guarded (`Py_BEGIN_CRITICAL_SECTION`, a `*_lock_held` wrapper, or an Argument Clinic
   > `@critical_section` on the impl) while another is not, emit
   > `type: "lazy_init_partial_guard"`, `ft_class: "T2"`, `confidence: "high"`, and cite the
   > guarded accessor's `file:line` in the detail as `guarded_twin`.

   This rule fires on `ga_getitem` (guarded twin `ga_parameters:858` in the same file) and on
   `func_get_annotation_dict` (guarded twin in `clinic/funcobject.c.h`), i.e. on both of the run's
   novel T2 findings, and it fires at high confidence with the fix already attached. It does **not**
   fire on `descr_get_qualname` (no twin in-file) — which is correct behaviour for a *new* finding,
   since that one is already catalogued as TSAN-0043.

3. **Suppress T1 and T3 candidates whose flagged access is inside a `#ifndef Py_GIL_DISABLED` /
   `#if !defined(Py_GIL_DISABLED)` region.** Track preprocessor conditionals during the tree-sitter
   walk (the C grammar exposes `preproc_ifdef` / `preproc_if` nodes). Removes **2/2 of the sample's
   T1 FPs** and prevents future FPs on `tupleiter_next` / `listiter_next` / `reversed_next`.
   Conversely, record `#ifdef Py_GIL_DISABLED`-only regions as *evidence of an existing fix* so the
   guarded-twin logic in proposal 2 can use them.

4. **Gate T2 on the owner being heap state.** Skip when the base variable of `X->field` is a local
   declared in the enclosing function (`unionbuilder ub;` at `unionobject.c:264`). Removes the
   third sample FP; takes T2 to 3/3 in this sample.

5. **Model Argument Clinic `@critical_section`.** When scanning `<dir>/<file>.c`, also parse
   `<dir>/clinic/<file>.c.h`; if the generated wrapper for `<fn>_impl` contains
   `Py_BEGIN_CRITICAL_SECTION`, treat `<fn>_impl` as guarded. Prerequisite for proposal 6 not
   producing a false-positive wave, and required for proposal 2's twin detection to see the
   `funcobject.c` case.

6. **Make the T2 condition matcher multi-line.** Scan from `if (` to the matching close paren rather
   than to end-of-line. Recovers `funcobject.c:885`.

7. **Turn `*_lock_held` from a suppression into a check.** For each `*_lock_held` / `*_locked`
   function, enumerate its callers; emit `lock_held_caller_unguarded` (CONSIDER) for any caller that
   contains neither `Py_BEGIN_CRITICAL_SECTION*`, nor `_Py_CRITICAL_SECTION_ASSERT_*`, nor
   `_PyEval_StopTheWorld`, nor a `LOCK_*` macro, nor a comment matching
   `/world is stopped|single.thread|finaliz/i`. Extend to *unnamed* de-facto lock-held helpers: a
   static function with ≥2 callers where ≥1 is guarded and ≥1 is not. Would surface finding 6.

8. **Report each `tp_iternext` as its own finding rather than folding a second function into
   `duplicate_locations`.** `calliter_iternext` (`iterobject.c:243`) is a different type with a
   different fix from `iter_iternext` (`:79`); collapsing them understates the class in
   `summary.by_class` and hides one of the two from any downstream consumer that reads only
   `findings[].line`.

9. **`data/cpython_non_bugs.md` — extend the free-threading FP section** with the two classes
   confirmed here:
   - *Stack-local aggregate lazy init.* `if (ub->field == NULL) ub->field = ...` where `ub` is a
     function-local struct is never a race (`Objects/unionobject.c:171`).
   - *`#ifndef Py_GIL_DISABLED` split path.* A plain access in the GIL-only arm of a preprocessor
     split is by construction unreachable on the free-threaded build
     (`Objects/tupleobject.c:1165`).
   - And add the inverse note to the *positives* side: a drop elided under
     `#ifndef Py_GIL_DISABLED` is a **fixed** T3, and its presence in a file is a guarded twin.

---

## Reproduction artifacts

All scripts are preserved in `<run>/repro/`.

| script | build | result |
|---|---|---|
| `t3_repro.py seqiter` | `ft_cpython` (FT+ASan, `PYTHON_GIL=0`) | `_Py_NegativeRefcount`, refcount **-2**, fatal |
| `t3_repro.py gaiter` | `ft_cpython` | **ASan SEGV** at `ga_iternext` → `Py_DECREF(NULL)` |
| `t3_repro.py calliter` | `ft_cpython` | 0/400 — shape confirmed statically only |
| `t2_repro.py ga` | `3.14_tsan_debug_ft` | **TSan data race** ×3 at `ga_getitem` / `ga_parameters` / `_Py_subs_parameters` |
| `t2_descr.py` | `3.14_tsan_debug_ft` | **TSan data race** at `descr_get_qualname:624/625` (TSAN-0043) |
| `t3sweep.py` | static | 13 `tp_iternext` drop sites across `Objects/` vs the scanner's 4 |
