# API deprecation + macro hygiene — `Objects/typeobject.c`

**Slice:** `obj-typeobject` (informed-explore)
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scope:** `Objects/typeobject.c` only, 13,068 lines
**Agent:** api-deprecation-tracker (+ macro-hygiene pass, not dispatched separately)

---

## Headline

The single scanner candidate is **real but misclassified in a way that matters**:
`PyEval_GetFrameGlobals` is **not** a drop-in replacement for `PyEval_GetGlobals`.
The old API returns a **borrowed** reference; the new one returns a **strong**
reference. The vocabulary says `drop_in: true` with an empty `caveat`, so a
maintainer following the scanner's own advice would introduce a reference leak on
every class creation. Same defect in the two sibling entries
(`PyEval_GetBuiltins`, `PyEval_GetLocals`) — all three PEP 667 entries are wrong
in the same direction.

Macro pass: **8 findings, none live.** The file's statement macros are
systematically missing `do {} while (0)`, but I checked every expansion and no
call site is an unbraced controlled statement, so every dangling-else is latent.
The slotdef table builders are *well* built — the `#NAME` / `_Py_ID(NAME)`
single-token derivation makes name drift structurally impossible — with one real
cross-file coupling worth removing.

---

## 1. The candidate: `deprecated-api PyEval_GetGlobals` @ `Objects/typeobject.c:4485`

### What it is doing there

The line sits in `type_new_set_module`, `Objects/typeobject.c:4474-4497` — a
`type_new` helper that supplies `__module__` for a class whose namespace does not
already define it. The prompt's guess was right: this is `__module__` resolution.

```c
/* Set __module__ in the dict */
static int
type_new_set_module(PyObject *dict)
{
    int r = PyDict_Contains(dict, &_Py_ID(__module__));
    if (r < 0) { return -1; }
    if (r > 0) { return 0; }              /* caller already set it */

    PyObject *globals = PyEval_GetGlobals();     /* <-- :4485, BORROWED */
    if (globals == NULL) { return 0; }           /* no frame: leave unset */

    PyObject *module;
    r = PyDict_GetItemRef(globals, &_Py_ID(__name__), &module);
    if (module) {
        r = PyDict_SetItem(dict, &_Py_ID(__module__), module);
        Py_DECREF(module);
    }
    return r;
}
```

The borrowed handle is consumed immediately by a `PyDict_GetItemRef` on a real
`dict` (a frame's `f_globals` is always an exact dict). No user code can run
between the borrow and the last use, so **the existing code is correct** — this is
not a borrowed-ref-across-call finding.

### Is `PyEval_GetFrameGlobals` a drop-in here? **No.**

The two implementations sit 120 lines apart in `Python/ceval.c` and differ in
exactly one line:

```c
/* Python/ceval.c:2744-2749 — the deprecated one */
PyObject *
PyEval_GetGlobals(void)
{
    PyThreadState *tstate = _PyThreadState_GET();
    return _PyEval_GetGlobals(tstate);       /* -> current_frame->f_globals, BORROWED */
}

/* Python/ceval.c:2868-2876 — the replacement */
PyObject* PyEval_GetFrameGlobals(void)
{
    PyThreadState *tstate = _PyThreadState_GET();
    _PyInterpreterFrame *current_frame = _PyThreadState_GetFrame(tstate);
    if (current_frame == NULL) {
        return NULL;
    }
    return Py_XNewRef(current_frame->f_globals);   /* STRONG */
}
```

`_PyEval_GetGlobals` (`Python/ceval.c:2734-2742`) returns `current_frame->f_globals`
raw. `PyEval_GetFrameGlobals` wraps the identical expression in `Py_XNewRef`.

Answer to each half of the decisive question:

| Axis | `PyEval_GetGlobals` | `PyEval_GetFrameGlobals` | Same? |
|---|---|---|---|
| Refcount | **borrowed** | **strong (`Py_XNewRef`)** | **NO** |
| No frame | returns `NULL`, no exception set | returns `NULL`, no exception set | yes |
| Frame lookup | `_PyThreadState_GetFrame(tstate)` | `_PyThreadState_GetFrame(tstate)` | yes |

So the *behavioural* half is clean — the `if (globals == NULL) return 0;` guard at
:4486 needs no change, and neither API sets an exception on the no-frame path.
The break is purely the refcount.

### What a mechanical swap would cost

Substituting the name at :4485 and nothing else leaks one strong reference to the
module's `__dict__` on **every class creation that reaches line 4485** — i.e. every
class whose body did not set `__module__` explicitly, which is nearly all of them.
A module `__dict__` transitively holds the entire module alive, so this is not a
small leak: it would pin every module that ever defines a class, for the process
lifetime. The correct migration is three lines, not one:

```c
    PyObject *globals = PyEval_GetFrameGlobals();   /* now a strong ref */
    if (globals == NULL) { return 0; }

    PyObject *module;
    r = PyDict_GetItemRef(globals, &_Py_ID(__name__), &module);
    Py_DECREF(globals);                              /* <-- REQUIRED, new */
    if (module) {
        r = PyDict_SetItem(dict, &_Py_ID(__module__), module);
        Py_DECREF(module);
    }
    return r;
```

### Corroboration from the tree itself

Two independent places in CPython already encode the asymmetry, which is why I am
confident this is not a doc-reading error:

- `Modules/_testlimitedcapi/eval.c:25` — `return Py_XNewRef(PyEval_GetGlobals());`
  The test wrapper must *add* `Py_XNewRef` to give `PyEval_GetGlobals` new-reference
  semantics. That is the delta, written out.
- `Python/import.c:4681` — `globals = PyEval_GetGlobals();  // borrowed`
  CPython annotates the call site with the word.

### Classification: **POLICY**

Per the run's calibration, CPython calling its own soft-deprecated API during a
transition is POLICY, not FIX. Reinforcing that here:

- `tier: soft` is correct — `Include/ceval.h:20-22` declares all three of
  `PyEval_GetBuiltins` / `GetGlobals` / `GetLocals` as plain `PyAPI_FUNC`, no
  `Py_DEPRECATED` marker. The build does not warn.
- `removed_in` is correctly empty — `grep -rn "PyEval_Get" Doc/deprecations/`
  returns **nothing**. No removal is scheduled, so nothing stops compiling.
- The code as written is correct. There is no bug at :4485.

**The finding is not the call. The finding is the recommendation attached to it.**

### Prior art: none

Searched via `gh api -X GET search/issues`. No issue or PR exists on migrating
CPython's internal callers off the PEP 667-deprecated reflection APIs, and
**nobody has recorded the borrowed→strong asymmetry** as a migration hazard.
Adjacent-but-different:

- **gh-118934** (closed) — `PyEval_GetLocals()` leaks locals; **gh-119769** fixed it
  by making `PyEval_GetLocals` return a *borrowed* reference. The reverse problem,
  inside one API rather than across the pair.
- **gh-125170** (open) — "PEP 667 follow-up: hard deprecate `PyEval_GetLocals()`",
  targeting removal in 3.16 (PR gh-128348). Covers `GetLocals` **only**, not
  `GetGlobals`/`GetBuiltins`, and has not landed — which is why `removed_in: ""`
  is still right for all three today. Worth re-checking at each release.
- `type_new_set_module` has **zero** tracker hits.

### Tree-wide reach of the same caveat

`typeobject.c:4485` is the only hit in `Objects/`, but there are **six more live
internal callers** that a mechanical migration would break identically:

| File:line | |
|---|---|
| `Objects/typeobject.c:4485` | this slice |
| `Python/import.c:3971` | |
| `Python/import.c:4681` | already annotated `// borrowed` |
| `Python/bltinmodule.c:315` | |
| `Python/bltinmodule.c:1067` | |
| `Python/bltinmodule.c:1180` | |
| `Python/bltinmodule.c:1361` | |

Plus `Modules/_testlimitedcapi/eval.c:25` (test wrapper — `compat_shim_files`
candidate) and the definition at `Python/ceval.c:2745`.

---

## 2. Vocabulary correction — `data/deprecated_c_apis.json`

**The entry is wrong.** Current state:

```json
{ "name": "PyEval_GetGlobals", "tier": "soft", "deprecated_in": "3.13",
  "removed_in": "", "replacement": "PyEval_GetFrameGlobals",
  "drop_in": true, "caveat": "", "compat_shim_files": [],
  "notes": "Doc/c-api/reflection.rst:48 (PEP 667)." }
```

`drop_in: true` is exactly the failure mode the schema's own docstring warns
about — it is the second instance of the `_PyUnicodeWriter_WriteStr` /
gh-148241 shape that motivated adding these two fields. **This is a systematic
defect across all three PEP 667 entries, not a one-off**: `PyEval_GetBuiltins`
even carries `"notes": "... Returns a borrowed reference."` while still asserting
`drop_in: true` — the evidence against the field is sitting in the same record.

### Proposed replacements

```json
{ "name": "PyEval_GetGlobals", "tier": "soft", "deprecated_in": "3.13",
  "removed_in": "", "replacement": "PyEval_GetFrameGlobals",
  "drop_in": false,
  "caveat": "NOT a rename. PyEval_GetGlobals returns a BORROWED reference (Python/ceval.c:2744-2749 -> _PyEval_GetGlobals returns current_frame->f_globals raw); PyEval_GetFrameGlobals returns a STRONG reference (Python/ceval.c:2868-2876, Py_XNewRef). Swapping the name alone leaks the frame's globals dict -- and a module __dict__ pins the whole module. The migration must add a Py_DECREF after the last use. NULL-on-no-frame behaviour IS identical and needs no change; neither sets an exception. Verified drop-in: none -- the fix is name swap + Py_DECREF.",
  "compat_shim_files": ["Modules/_testlimitedcapi/eval.c"],
  "notes": "Doc/c-api/reflection.rst:46-53 (PEP 667). Declared without Py_DEPRECATED at Include/ceval.h:21, so the build is silent. No entry in Doc/deprecations/ -- nothing is scheduled for removal. 7 live internal callers at 4f3be1b5: Objects/typeobject.c:4485, Python/import.c:3971 and :4681, Python/bltinmodule.c:315/1067/1180/1361." }

{ "name": "PyEval_GetBuiltins", "tier": "soft", "deprecated_in": "3.13",
  "removed_in": "", "replacement": "PyEval_GetFrameBuiltins",
  "drop_in": false,
  "caveat": "NOT a rename. Same borrowed-vs-strong split as PyEval_GetGlobals: PyEval_GetFrameBuiltins is `return Py_XNewRef(_PyEval_GetBuiltins(tstate));` (Python/ceval.c:2878-2882) while PyEval_GetBuiltins returns the borrowed inner result. Migration must add a Py_DECREF. Python/ceval.c:2815 annotates its own call `// borrowed` and compensates with an explicit Py_INCREF at :2820 -- a mechanical swap there would double-count.",
  "compat_shim_files": [],
  "notes": "Doc/c-api/reflection.rst:10 (PEP 667). Include/ceval.h:20, no Py_DEPRECATED." }

{ "name": "PyEval_GetLocals", "tier": "soft", "deprecated_in": "3.13",
  "removed_in": "", "replacement": "PyEval_GetFrameLocals",
  "drop_in": false,
  "caveat": "NOT a rename, and the worst of the three -- it differs in BOTH refcount and semantics. PyEval_GetLocals returns a borrowed reference backed by a per-frame cache dict kept alive solely for this API (see the comment at Include/internal/pycore_frame.h:27-28). PyEval_GetFrameLocals returns a STRONG reference AND, in optimized scopes, an INDEPENDENT SNAPSHOT -- mutating the result no longer affects the frame. Migration needs a Py_DECREF plus a check that no caller relies on write-through. To read f_locals without snapshotting, call PyFrame_GetLocals on PyEval_GetFrame() instead (Doc/c-api/reflection.rst:73-77). Tracker: gh-118934 / gh-119769 (the borrowed-ref fix), gh-125170 (open, proposes hard-deprecating this one with removal in 3.16 -- recheck removed_in each release).",
  "compat_shim_files": [],
  "notes": "Doc/c-api/reflection.rst:20 (PEP 667). Include/ceval.h:22, no Py_DEPRECATED." }
```

### Toolkit-level recommendation

Three of three PEP 667 entries carried `drop_in: true` incorrectly. The
generalisable rule, worth applying as an audit across the remaining 90 entries:

> **When the replacement's name signals a different object-acquisition
> (`Get` → `GetFrame`, `Get` → `GetRef`, `Borrow` → `New`), assume `drop_in:
> false` until the two implementations have been read side by side.** The
> PEP 667 pair, `PyDict_GetItem`→`PyDict_GetItemRef`, and
> `PyModule_AddObject`→`PyModule_AddObjectRef` are all the same shape: the
> deprecation exists *because* the refcount contract changed, so `drop_in: true`
> on such a pair is a contradiction in terms.

A cheap mechanical canary: flag any entry where `replacement` contains `Ref`,
`Frame`, or `New` and `drop_in` is `true`.

---

## 3. Macro hygiene pass

**Census: 69 `#define` directives.** Composition:

| Group | Lines | Count |
|---|---|---|
| FT lock / assertion helpers + their GIL-build no-op twins | 48-200 | 27 |
| `COPY*` / `SLOTDEFINED` inheritance helpers | 8761-8844 | 9 |
| Slot-wrapper function definers (`SLOT0/1/1BIN/1BINFULL`, `RICHCMP_WRAPPER`) | 10265-10617 | 5 |
| Slotdef table builders (`TPSLOT`…`RBINSLOTNOTINFIX`) | 11547-11583 | 14 |
| Constants, casts, local helpers (`CHECK`, `NO_SPEC`, `*_CAST`, `MCACHE_*`) | scattered | 14 |

`slotdefs[]` (`:11585-11782`) contains **94 entries**; `inherit_slots`
(`:8822-9015`) contains **66 `COPY*` expansions**.

### Live defects: **zero**

I mechanically checked every expansion of every statement macro
(`ASSERT_TYPE_LOCK_HELD`, `ASSERT_NEW_TYPE_OR_LOCKED`,
`ASSERT_WORLD_STOPPED_OR_NEW_TYPE`, `COPYSLOT`, `COPYNUM`, `COPYSEQ`, `COPYMAP`,
`COPYBUF`, `COPYASYNC`, `COPYVAL`, `BEGIN_TYPE_DICT_LOCK`, `BEGIN/END_TYPE_LOCK`)
for the shape "macro is the unbraced controlled statement of an `if`/`else`/
`for`/`while`". **0 hits.** The prompt's worry — an unhygienic slotdef macro
mis-wiring a slot for every type — does not materialise; the slotdef builders are
the *best*-constructed macros in the file. Everything below is latent.

---

### M1 — `ASSERT_TYPE_LOCK_HELD()` / `ASSERT_NEW_TYPE_OR_LOCKED()`: bare `if`, no `do-while`, and empty in release — CONSIDER

`Objects/typeobject.c:108-109` and `:115-116`, empty twins at `:118-120` and `:188-191`.

```c
#define ASSERT_TYPE_LOCK_HELD() \
    if (!types_world_is_stopped()) { _Py_CRITICAL_SECTION_ASSERT_MUTEX_LOCKED(TYPE_LOCK); }

#define ASSERT_NEW_TYPE_OR_LOCKED(tp) \
    if (TYPE_IS_REVEALED(tp)) { ASSERT_TYPE_LOCK_HELD(); }
```

Two distinct latent hazards, and the second is the nastier one:

1. **Dangling else.** `if (c) ASSERT_TYPE_LOCK_HELD(); else X;` binds `else` to the
   macro's inner `if`. `ASSERT_NEW_TYPE_OR_LOCKED` nests two such bare `if`s.
2. **Build-dependent empty expansion.** In a non-`Py_DEBUG` build these expand to
   *nothing* (`:118-120`), so `if (c) ASSERT_TYPE_LOCK_HELD();` becomes `if (c) ;`
   and the following statement silently escapes the conditional. A defect that
   exists only in release builds is the worst kind to find later — and RUN_CONTEXT
   already flags debug-vs-release divergence as a measured trap for this campaign.

`ASSERT_WORLD_STOPPED_OR_NEW_TYPE` (`:112-113`) is **not** affected — it expands to
`assert(...)`, which is already safe in an unbraced `if`/`else`.

**The file contains its own guarded twin.** `_PyType_CheckConsistency`'s local
`CHECK` at `:868-869` is written correctly:

```c
#define CHECK(expr) \
    do { if (!(expr)) { _PyObject_ASSERT_FAILED_MSG(...); } } while (0)
```

Fix: wrap both assertion macros in `do { ... } while (0)` and give the release-build
stubs `do { } while (0)` bodies. Established idiom, same file, ~6 lines.

---

### M2 — `COPYSLOT` is a bare-`if` macro; its sibling `COPYVAL` is braced — CONSIDER

`:8837-8838` vs `:8761-8762`.

```c
#define COPYVAL(SLOT) \
    if (type->SLOT == 0) { type->SLOT = base->SLOT; }        /* :8761 — braced */

#define COPYSLOT(SLOT) \
    if (!type->SLOT && SLOTDEFINED(SLOT)) type->SLOT = base->SLOT   /* :8837 — not */
```

Two macros 76 lines apart doing the same job with **asymmetric failure modes**:

- `COPYVAL(x);` inside an unbraced `if`/`else` is a **syntax error** — the trailing
  `;` after the braced body orphans the `else`. Fail-loud.
- `COPYSLOT(x);` inside an unbraced `if`/`else` **compiles and misbinds** the `else`
  to the macro's inner `if`. Fail-silent.

66 expansions, all currently safe. The nearest miss is `COPYSLOT(tp_free)` at
`:8998`, which sits directly before an `else if` at `:9000` — and is saved only
because the author happened to brace it at `:8996-8999`. That is one deleted pair
of braces away from a silent slot-inheritance bug.

Fix: `do { ... } while (0)` on `COPYSLOT`, matching `CHECK`.

---

### M3 — `SLOTDEFINED`/`COPYSLOT` capture the *mutable* local `basebase` — CONSIDER

`:8833-8838`, expanded 66 times across `:8846-9013`.

```c
#define SLOTDEFINED(SLOT) \
    (base->SLOT != 0 && \
     (basebase == NULL || base->SLOT != basebase->SLOT))
```

Three implicit captures — `type`, `base`, and `basebase` — of which **`basebase` is
declared uninitialized at `:8824` and reassigned six times** through the function
(`:8850, 8891, 8900, 8914, 8923, 8930`). Each expansion's meaning therefore depends
on which assignment last executed at that point, with the definition 100+ lines away
from most call sites. This is the highest-consequence hygiene shape in the file.

**Verified safe today:** every one of the 66 expansions is dominated by an
assignment to `basebase`, so there is no uninitialized read. But the invariant is
maintained by nothing except reading the function top to bottom.

Related latent NULL deref, same construct, five instances
(`:8850-8851, 8891-8892, 8900-8901, 8914-8915, 8923-8924`):

```c
        basebase = base->tp_base;
        if (basebase->tp_as_number == NULL)     /* :8851 — no NULL check on basebase */
```

`base->tp_base` is `NULL` only for `object`, and the enclosing guard at `:8849`
(`base->tp_as_number != NULL`) excludes `object` because
`PyBaseObject_Type.tp_as_number` is `NULL`. So it is guarded — but by a
**non-local invariant about a different type**, not by anything visible at :8851.
Contrast the tail path at `:8930`, which assigns `basebase = base->tp_base;` with
no check and is genuinely safe, because there `SLOTDEFINED`'s own
`basebase == NULL ||` short-circuits first.

Classification: **CONSIDER** for the capture (a comment naming the three captured
locals and the `basebase` protocol would cost two lines); **ACCEPTABLE** for the
NULL deref, which is correct but under-documented.

---

### M4 — `COPYASYNC` missing from the defensive `#undef` prologue — ACCEPTABLE

`inherit_slots` opens (`:8826-8831`) by undef'ing six of the seven macros it is
about to define:

```c
#undef SLOTDEFINED
#undef COPYSLOT
#undef COPYNUM
#undef COPYSEQ
#undef COPYMAP
#undef COPYBUF
        /* COPYASYNC (:8840) is absent — and never #undef'd anywhere in the file */
```

Harmless today: nothing else in the translation unit defines `COPYASYNC`, and a
future collision would surface as a loud macro-redefinition diagnostic, not a
silent miswiring. Pure consistency gap — the async slots (3.5) postdate the
prologue. One-line fix.

---

### M5 — slotdef table builders: structurally sound, one cross-file coupling — CONSIDER

`:11547-11583`, driving all 94 `slotdefs[]` entries.

**The good part, and it is genuinely good.** `TPSLOT(NAME, SLOT, FUNCTION, WRAPPER, DOC)`
derives *both* the C string `#NAME` and the interned `&_Py_ID(NAME)` from one token:

```c
#define TPSLOT(NAME, SLOT, FUNCTION, WRAPPER, DOC) \
    {#NAME, offsetof(PyTypeObject, SLOT), (void *)(FUNCTION), WRAPPER, \
     PyDoc_STR(DOC), .name_strobj = &_Py_ID(NAME)}
```

The table's name and its interned twin **cannot drift** — exactly the property you
want from a stringly-typed table builder, and the specific class of mis-wiring the
prompt was worried about. `FUNCTION` is parenthesized. The 11 derived wrappers
(`BUFSLOT`/`AMSLOT`/`SQSLOT`/`MPSLOT`/`NBSLOT`/`UNSLOT`/`IBSLOT`/`BINSLOT`/
`RBINSLOT`/`BINSLOTNOTINFIX`/`RBINSLOTNOTINFIX`) forward to `ETSLOT` and add only
doc-string literals. All 14 are `#undef`'d after the table.

**The fragility.** `TPSLOT`/`ETSLOT` initialize the first **five** fields of
`struct wrapperbase` **positionally**, `FLSLOT` the first **six**, before switching
to a designated `.name_strobj`:

```c
/* Include/cpython/descrobject.h:11-19 */
struct wrapperbase {
    const char *name;      /* 1 positional */
    int offset;            /* 2 positional */
    void *function;        /* 3 positional */
    wrapperfunc wrapper;   /* 4 positional */
    const char *doc;       /* 5 positional */
    int flags;             /* 6 positional, FLSLOT only */
    PyObject *name_strobj; /*   designated  */
};
```

Reordering or inserting a field in `struct wrapperbase` — a **public** struct in
`Include/cpython/`, edited by people who are not editing `typeobject.c` — silently
mis-wires all 94 slotdef entries, i.e. every type in the interpreter. This is the
"mis-wires a slot for every type" blast radius, but the trigger lives in a
different file, which is what makes it worth writing down: nobody editing
`descrobject.h` will think to check `typeobject.c`.

Fix: convert the remaining five/six positional initializers to designated ones.
Mechanical, three macros, removes the cross-file coupling entirely. The lone
`.name_strobj` already present shows the intended idiom.

---

### M6 — `NO_SPEC` contains a `goto` to a caller-defined label — ACCEPTABLE

`:5305-5313`, expanded 3 times inside a `switch`.

```c
    #define NO_SPEC                                         \
        if (spec) {                                         \
            PyErr_Format(PyExc_SystemError,                 \
                "%s must not be used with PyType_Spec",     \
                _PySlot_GetName(it.current.sl_id));         \
            goto finally;                                   \
        }                                                   \
        /////////////////////////////////////////////////////
```

Captures `spec`, `it`, **and the caller's `finally:` label** — a `goto` inside a
macro to a label defined in the expanding function is the strongest form of
implicit capture there is. Mitigating: function-local, correctly `#undef`'d, three
call sites all within ~15 lines of the definition, so the capture is legible in
context. Not `do-while` wrapped, but it ends in `}`, so `NO_SPEC;` in an unbraced
`if`/`else` is a **compile error** — fail-loud. No action needed.

---

### M7 — `MCACHE_CACHEABLE_NAME` double-evaluates its argument — ACCEPTABLE

`:56-58`. `name` is unparenthesized and appears twice:

```c
#define MCACHE_CACHEABLE_NAME(name)                             \
        (PyUnicode_CheckExact(name) &&                          \
         (PyUnicode_GET_LENGTH(name) <= MCACHE_MAX_ATTR_SIZE))
```

Both call sites (`:6302`, `:6374`) pass a bare local, so no live double evaluation.
Latent. Sibling `MCACHE_HASH` (`:49-51`) and `MCACHE_HASH_METHOD` (`:53-55`) are
correctly parenthesized and single-evaluation.

---

### M8 — identifier-rewriting and argument-discarding macros — ACCEPTABLE

- **`#define slot_mp_length slot_sq_length` (`:10713`), never `#undef`'d.** Silently
  rewrites that identifier for the remaining ~2,350 lines. One consumer, `:11741`.
  Intentional (both are `lenfunc`) but it is literally the "macro that shadows an
  identifier used at the call site" shape. A `#undef` after `:11741` costs nothing.
- **`BEGIN_TYPE_DICT_LOCK(d)` discards `d` entirely in the GIL build** (`:186` vs
  `:82-83`); `TYPE_IS_REVEALED(tp)` discards `tp` in the GIL and 32-bit builds
  (`:104`, `:189`). A side-effecting argument would execute only under
  `Py_GIL_DISABLED`. All three call sites (`:6829`, `:113`, `:116`) pass bare
  variables. Latent.
- **`TYPE_LOCK` (`:78`) has no outer parentheses** —
  `&_PyInterpreterState_GET()->types.mutex`. Safe in practice: unary `&` on a
  postfix expression binds tighter than every binary operator, so the equality
  comparisons at `:150`, `:155`, `:159` parse correctly. It does re-invoke
  `_PyInterpreterState_GET()` at each expansion (twice in
  `type_lock_prevent_release`), which is side-effect-free. Cosmetic; PEP 7 reviewers
  would ask for the parens.

---

## 4. Deprecated APIs the scanner missed: **none**

I swept **all 93 vocabulary entries** by word boundary against the full 13,068
lines. Exactly **one** match: `PyEval_GetGlobals` at `:4485` — the reported line.

The denominator is real, so this is an earned zero, not a structural one: the
vocabulary is non-empty, the sweep is independent of the scanner, and it recovered
the known finding. Recall on this file is 1/1.

Corpus context from RUN_CONTEXT: 141 `deprecated-api` findings across `Objects/`,
of which this slice contributes 1. The rule is high-volume tree-wide but nearly
silent here — `typeobject.c` is not a deprecated-API hotspot.

---

## 5. Classification summary

| # | Finding | File:line | Class |
|---|---|---|---|
| 1 | `PyEval_GetGlobals` call — correct as written; CPython may call its own soft-deprecated API | `Objects/typeobject.c:4485` | **POLICY** |
| 2 | Vocabulary asserts `drop_in: true`; replacement returns a strong ref, so the advice leaks | `data/deprecated_c_apis.json` (`PyEval_GetGlobals`) | **FIX (toolkit)** |
| 3 | Same defect in `PyEval_GetBuiltins` and `PyEval_GetLocals` entries | `data/deprecated_c_apis.json` | **FIX (toolkit)** |
| M1 | Assertion macros: bare `if`, no `do-while`, empty in release | `:108-109`, `:115-116` | CONSIDER |
| M2 | `COPYSLOT` bare `if` while sibling `COPYVAL` is braced (asymmetric fail-silent) | `:8837-8838` | CONSIDER |
| M3 | `SLOTDEFINED`/`COPYSLOT` capture mutable uninitialized-at-declaration `basebase` | `:8824`, `:8833-8838` | CONSIDER |
| M5 | Slotdef builders positionally initialize a public struct in another header | `:11547-11555` | CONSIDER |
| M4 | `COPYASYNC` missing from the `#undef` prologue | `:8826-8831`, `:8840` | ACCEPTABLE |
| M6 | `NO_SPEC` captures `spec`, `it`, and the `finally:` label | `:5305-5313` | ACCEPTABLE |
| M7 | `MCACHE_CACHEABLE_NAME` double-evaluates its argument | `:56-58` | ACCEPTABLE |
| M8 | `slot_mp_length` alias never `#undef`'d; arg-discarding GIL-build stubs; `TYPE_LOCK` parens | `:10713`, `:186/189`, `:78` | ACCEPTABLE |

**Nothing here is a FIX in CPython.** The one FIX is in the toolkit's own data
file, and it is the finding worth acting on: a scanner that confidently recommends
a reference leak on a hot path is worse than one that says nothing.

---

## 6. Toolkit assessment

**Precision, `scan_deprecated_apis` on this slice: 1/1 true positive.** The API is
genuinely soft-deprecated, the call site is genuine (not a definition site, not a
comment, not a substring artifact), and the metadata is right on `tier`,
`deprecated_in`, `removed_in`, and `severity`. The verified-against-the-tree
methodology in `_meta` is holding — this is not the old hand-grepped list.

**Recall on this slice: 1/1.** Independent sweep of the full vocabulary found
nothing extra.

**The defect is in a field, not in detection.** `drop_in`/`caveat` were added
precisely to stop the scanner from recommending a regression, and then three PEP 667
entries were populated with `drop_in: true` anyway — one of them
(`PyEval_GetBuiltins`) with `"Returns a borrowed reference"` sitting in its own
`notes`. The schema is right; the data was not audited against it.

Concrete proposals:

1. **Apply the three corrected entries above.**
2. **Audit the remaining 90 entries against the schema's own intent.** Cheap canary:
   any entry whose `replacement` contains `Ref`, `Frame`, or `New` while
   `drop_in` is `true` is suspect by construction — the rename exists *because* the
   acquisition contract changed.
3. **Add a validator to the test suite**: assert that `drop_in: false` implies a
   non-empty `caveat` (the schema already says "Required when drop_in is false"),
   and surface `findings_needing_a_caveat` in the summary — the field already exists
   in the JSON envelope and read `0` here, which was a false all-clear.
4. **Add `Modules/_testlimitedcapi/eval.c` to `compat_shim_files`** for the PEP 667
   entries. Its `Py_XNewRef(PyEval_GetGlobals())` is a deliberate test wrapper, and
   it doubles as the tree's own proof of the refcount delta.
5. **Recheck `PyEval_GetLocals.removed_in` each release** — gh-125170 proposes hard
   deprecation with removal in 3.16 and has not landed. If it does, that entry moves
   from CONSIDER to FIX and the caveat becomes load-bearing.
6. **Macro-hygiene rule to add** (this pass was manual; a scanner could do it):
   flag a `#define` whose body starts with `if` and is not wrapped in
   `do { } while (0)`, then *rank by whether any expansion is the unbraced
   controlled statement of an `if`/`else`/loop*. That ranking is what separates the
   8 latent findings here from a real bug, and it is what keeps such a rule from
   drowning `Objects/` in style noise.
