# macro-hygiene-reviewer — `Objects/typeobject.c` PASS 2

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e`
**Scope:** `Objects/typeobject.c` (13,068 lines) + `Include/internal/pycore_typeobject.h`,
`pycore_object.h`, `pycore_critical_section.h`, `pycore_lock.h`. Two adjacent headers are cited
where a macro defined there is load-bearing *in this file*: `pycore_pyatomic_ft_wrappers.h`
and `pycore_stats.h`.

**Provenance check (run rule).** `git diff 4f3be1b5777 a1d580430c8 --` over all five in-scope
files is **empty** — the build-matrix binaries at `a1d580430c8` are built from byte-identical
copies of every file cited below. No build/source skew applies to anything in this report.

**Method.** Full `#define`/`#undef` inventory of each file (script:
`scratchpad/macro_inv.py`), expansion ground-truth via `gcc -E` on the configured
`debug-ft-nojit` and `debug-gil-nojit` trees, call-site counts by anchored grep, and a direct
`tree_sitter_utils.extract_functions()` probe to measure what the toolkit's own parser sees.

**Totals.** 57 distinct macro names in `typeobject.c` (86 `#define` directives across the
build-variant branches); 16 in `pycore_typeobject.h`; 14 in `pycore_critical_section.h`; 3 in
`pycore_lock.h`; 35 in `pycore_object.h`.

---

# PART 1 — Classic macro hygiene

## Summary

- Macros reviewed: **125** definitions across the five in-scope files.
- Hygiene findings: **11** (1 FIX, 7 CONSIDER, 3 POLICY).
- Clean negatives established: header guards (4/4), parenthesisation of every arithmetic macro
  in `typeobject.c`, no live dangling-else instance, no live multiple-evaluation instance.

---

## FIX

### [FIX] `_Py_IsImmortalLoose(op)` expands to a function *address*, not a call — `Include/internal/pycore_object.h:31`

**What.**

```c
/* For backwards compatibility -- Do not use this */
#define _Py_IsImmortalLoose(op) _Py_IsImmortal
```

The parameter `op` is declared but never appears in the replacement list, and the replacement
is the bare token `_Py_IsImmortal` with **no argument list**. `_Py_IsImmortal` is a
`static inline int` function (`Include/refcount.h:126`) plus a function-like alias macro
(`refcount.h:137`). Because the expansion is not followed by `(`, the alias macro does not
re-trigger; the token decays to the *address* of the inline function.

**Risk.** `if (_Py_IsImmortalLoose(op))` is unconditionally true. `assert(_Py_IsImmortalLoose(x))`
never fails. The argument `op` is never evaluated, so any side effect in it is silently dropped.
`-Waddress` fires on the boolean form, but nothing rejects the macro at its definition site, and
a use in a non-boolean context (`x = _Py_IsImmortalLoose(op)`) fails with a confusing
pointer-to-int diagnostic pointing at the *call* site, not the broken definition.

**Mitigating.** Confirmed **zero users** tree-wide (`grep -rn '_Py_IsImmortalLoose' --include=*.c
--include=*.h .` returns only the definition). This is dead code in an internal, `Py_BUILD_CORE`-only
header — so the classification is "broken definition present", not "live miscompile".

**Fix.** Delete it (it is documented as backwards-compat-only and has no in-tree consumer), or
correct it to `#define _Py_IsImmortalLoose(op) _Py_IsImmortal(op)`. Deletion is preferable: the
comment already says "Do not use this", and a header-only compatibility shim with no users and a
wrong body is strictly worse than nothing.

---

## CONSIDER

### [CONSIDER] `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(op)` — bare `if`, no `do/while(0)`, and the parse changes with the build — `Include/internal/pycore_critical_section.h:64` / `:71` / `:80`

**What.**

```c
#ifdef Py_DEBUG
# define _Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(op)   \
    if (Py_REFCNT(op) != 1) {                            \
        _PyCriticalSection_AssertHeldObj(_PyObject_CAST(op)); \
    }
#else
# define _Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(op)
#endif
```

Three defects in one macro:

1. **Not `do { … } while (0)`-wrapped.** In a `Py_DEBUG` build the expansion is an `if` statement
   with a dangling tail; in a release build it is *empty*. `if (c) MACRO(o); else foo();` binds
   `else` to the macro's own `if` under `Py_DEBUG` and to the caller's `if` in release — the same
   source parses to two different programs depending on `-DPy_DEBUG`.
2. **`op` is evaluated twice** — once by `Py_REFCNT(op)`, once by `_PyObject_CAST(op)` — and
   zero times in release. Any argument with a side effect behaves three different ways.
3. `op` is unparenthesised at both use points; both consumers happen to parenthesise internally,
   so this one is latent rather than live.

**Blast radius.** **125 call sites** tree-wide (`listobject.c`, `odictobject.c`, `setobject.c`,
`codeobject.c`, …). I checked every one that is not at statement position; all are either
statement-position at the top of a `*_lock_held` function or already inside braces
(`setobject.c:1192-1196` is the closest — the macro sits inside a braced `if` under `#ifdef
Py_DEBUG`, so it is safe). **No live dangling-else instance exists today.**

**Guarded twin.** The sibling macro two lines up, `_Py_CRITICAL_SECTION_ASSERT_MUTEX_LOCKED(mutex)`
(`:47`), expands to a *single function call expression*, so `MACRO(x);` is a well-formed
statement in every position and every build. It is the correct shape.

**Fix.** `do { if (Py_REFCNT(op) != 1) { … } } while (0)` for the debug arm and
`do { } while (0)` for the release arm — matching what `_PyReftracerTrack` (`pycore_object.h:111`)
already does correctly in the same family.

---

### [CONSIDER] `ASSERT_TYPE_LOCK_HELD()` and `ASSERT_NEW_TYPE_OR_LOCKED(tp)` inherit the same bare-`if` shape — `Objects/typeobject.c:108` / `:115`

**What.**

```c
#define ASSERT_TYPE_LOCK_HELD() \
    if (!types_world_is_stopped()) { _Py_CRITICAL_SECTION_ASSERT_MUTEX_LOCKED(TYPE_LOCK); }

#define ASSERT_NEW_TYPE_OR_LOCKED(tp) \
    if (TYPE_IS_REVEALED(tp)) { ASSERT_TYPE_LOCK_HELD(); }
```

Same defect as above, one layer down, and `ASSERT_NEW_TYPE_OR_LOCKED` nests it. Both are empty
in the non-`Py_DEBUG` arm (`:118`/`:120`) and empty again in the whole GIL build (`:188`/`:191`),
so the same source parses three ways across the four supported build configurations.

**Call sites checked.** `ASSERT_TYPE_LOCK_HELD` — 16 direct sites (1295, 1410, 1751, 1800, 1931,
3433, 3596, 3657, 6739, 9248, 9323, 9530, 10126, 12096, 12162, 12526) plus one nested use inside
`ASSERT_NEW_TYPE_OR_LOCKED`. `ASSERT_NEW_TYPE_OR_LOCKED` — 4 sites (602, 1183, 11910, 12075).
`ASSERT_WORLD_STOPPED_OR_NEW_TYPE` — 2 sites (457, 464); that one *is* correctly shaped, since its
body is a single `assert(...)` expression statement.

Every one of the 22 sites is at statement position at the head of a function or directly after a
statement. **No live dangling-else.** Latent only.

**Fix.** `do { … } while (0)` on both, and on the empty arms. `ASSERT_WORLD_STOPPED_OR_NEW_TYPE`
is the in-file guarded twin showing the correct shape.

---

### [CONSIDER] `COPYSLOT(SLOT)` — unbraced `if` body, no `do/while(0)`, no terminating semicolon; 69 expansions — `Objects/typeobject.c:8837`

**What.**

```c
#define SLOTDEFINED(SLOT) \
    (base->SLOT != 0 && \
     (basebase == NULL || base->SLOT != basebase->SLOT))

#define COPYSLOT(SLOT) \
    if (!type->SLOT && SLOTDEFINED(SLOT)) type->SLOT = base->SLOT
```

`COPYSLOT` and its five aliases (`COPYASYNC`/`COPYNUM`/`COPYSEQ`/`COPYMAP`/`COPYBUF`, `:8840-8844`)
expand to a *bare, unbraced* `if` with the assignment as the controlled statement. 69 expansions
inside `inherit_slots`.

**Risk.** The classic dangling-else. The near-miss is at `:8995-8999`:

```c
        if ((type->tp_flags & Py_TPFLAGS_HAVE_GC) ==
            (base->tp_flags & Py_TPFLAGS_HAVE_GC)) {
            /* They agree about gc. */
            COPYSLOT(tp_free);
        }
        else if (…) {
```

The braces are the only thing keeping `else if` attached to the outer `if`. Remove them — a
plausible "single statement, drop the braces" cleanup, and PEP 7 permits brace-less single
statements — and `else if` silently reattaches to `COPYSLOT`'s inner `if`, so `tp_free` inheritance
and the GC-mismatch fixup both change behaviour with no diagnostic.

**Second defect in the same pair: `SLOTDEFINED` reads two caller locals it does not name.**
`base` and `basebase` are function-scope variables of `inherit_slots`; `basebase` is **reassigned
six times** (`:8850`, `:8891`, `:8900`, `:8914`, `:8923`, `:8930`), each time immediately before a
run of `COPY*` expansions. The macro's meaning therefore depends on *position in the function*, and
nothing in the source of a `COPYNUM(nb_add);` line indicates which `basebase` it reads. `basebase`
is also declared uninitialised at `:8824`; correctness rests entirely on every `COPY*` run being
preceded by an assignment. That holds today — I traced all six blocks — but it is enforced by
convention only.

**Third, and relevant to Part 2:** `SLOTDEFINED` contains the **only NULL guard on `basebase`**
(`basebase == NULL ||`). At all 69 `COPY*` call sites there is no visible NULL check, so a
null-safety scanner reading the call sites sees 69 unguarded uses of a pointer whose guard is
inside a macro one hop away.

**Fix.** `do { if (…) { … } } while (0)` for `COPYSLOT`, and pass `basebase` explicitly:
`SLOTDEFINED(BASEBASE, SLOT)`. `COPYVAL` (`:8761`) has the same unwrapped shape but is braced and
correctly `#undef`'d at `:8768` — it is the in-file guarded twin for the scoping half.

---

### [CONSIDER] `NO_SPEC` hides `goto finally` and captures two caller locals — `Objects/typeobject.c:5305`

**What.**

```c
    #define NO_SPEC                                         \
        if (spec) {                                         \
            PyErr_Format(                                   \
                PyExc_SystemError,                          \
                "%s must not be used with PyType_Spec",     \
                _PySlot_GetName(it.current.sl_id));         \
            goto finally;                                   \
        }                                                   \
        /////////////////////////////////////////////////////
```

Three things at once: (a) a **`goto` inside a macro expansion**, so seven `NO_SPEC;` lines
(`:5320`, `:5324`, `:5328`, `:5340`, `:5350`, `:5360`, `:5370`) are invisible control-flow exits
from a `switch` inside a `while`; (b) it reads the caller locals `spec` and `it` without naming
them; (c) the trailing `/////…` line is a line-continuation absorber — the comment is replaced by
a space in translation phase 3, *after* splicing in phase 2, so the body ends cleanly. Clever, but
it means the macro's real extent is not obvious from reading it.

**Mitigating.** Correctly `#undef`'d at `:5433`, so the scope is 128 lines. All seven uses are at
statement position inside `case` arms. Also a bare `if` with no `do/while(0)`, so the dangling-else
risk applies here too.

**Fix.** `do { … } while (0)` and drop the comment trick; the `goto` is defensible in a
Clinic-style bail-out but should be signalled in the macro name (`NO_SPEC_OR_FAIL`).

---

### [CONSIDER] `MCACHE_CACHEABLE_NAME(name)` evaluates `name` twice — `Objects/typeobject.c:56`

```c
#define MCACHE_CACHEABLE_NAME(name)                      \
        (PyUnicode_CheckExact(name) &&                   \
         (PyUnicode_GET_LENGTH(name) <= MCACHE_MAX_ATTR_SIZE))
```

`name` appears twice and is unparenthesised at both. Two call sites (`:6302` in
`should_assign_version_tag`, and one in pass-1 territory), both passing a plain parameter — no live
defect. The unparenthesised parameter is harmless because both uses sit in function-call argument
position. The double evaluation is the real shape.

Its sibling `MCACHE_HASH_METHOD` (`:53`) is the guarded twin: both parameters parenthesised, each
used exactly once.

**Fix.** `((void)0, PyUnicode_CheckExact(name) && …)` does not help; the correct fix is a
`static inline int mcache_cacheable_name(PyObject *name)`.

---

### [CONSIDER] `_Py_ClearImmortal(op)` evaluates and *writes through* its argument — `Include/internal/pycore_object.h:225`

```c
#define _Py_ClearImmortal(op) \
    do { \
        _Py_ClearImmortal(_PyObject_CAST(op)); \
        op = NULL; \
    } while (0)
```

Correctly `do/while(0)`-wrapped, but `op` is **evaluated twice and assigned to**, so the macro
silently requires an lvalue and rejects any expression with a side effect. `op = NULL` is also
unparenthesised, so `_Py_ClearImmortal(cond ? a : b)` is a syntax error rather than a diagnostic.

**In-scope call sites:** `typeobject.c:634` (`_Py_ClearImmortal(self->tp_bases)`) and `:689`
(`_Py_ClearImmortal(self->tp_mro)`). `self` is a plain parameter, so no live defect — but note the
consequence for Part 2: these are **the only two writes to `tp_bases`/`tp_mro` in the file that do
not go through `set_tp_bases`/`set_tp_mro`, and the write is inside the macro body.**

---

### [CONSIDER] `SIGNATURE_END_MARKER_LENGTH` is a hand-maintained duplicate of the marker's length — `Objects/typeobject.c:845-846`

```c
#define SIGNATURE_END_MARKER         ")\n--\n\n"
#define SIGNATURE_END_MARKER_LENGTH  6
```

The literal is 6 characters, so the pair is correct today. But `skip_signature` (`:851`) uses the
constant both for `strncmp` and for the returned offset (`return doc + SIGNATURE_END_MARKER_LENGTH;`),
so if the marker is ever edited without the constant, `skip_signature` returns a pointer *into* or
*past* the marker with no diagnostic. This is a docstring-introspection path reachable from
`__text_signature__`.

**Fix.** `#define SIGNATURE_END_MARKER_LENGTH (sizeof(SIGNATURE_END_MARKER) - 1)` — self-maintaining
and a compile-time constant.

---

## POLICY

### [POLICY] `TYPE_LOCK` is an ALL-CAPS name that expands to a function call — `Objects/typeobject.c:78`

```c
#define TYPE_LOCK &_PyInterpreterState_GET()->types.mutex
```

The name reads as a constant; the expansion is `&_PyInterpreterState_GET()->types.mutex`, i.e. a
call to `_PyInterpreterState_GET()` at every textual occurrence. It appears **four times** inside
`type_lock_prevent_release` (`:150`, `:155`, `:159`) and twice in `type_lock_allow_release`
(`:173`, `:178`) — up to four TLS reads per invocation where one would do. Also unparenthesised,
and the expansion begins with unary `&`; every current use is either an equality comparison, an
assignment RHS, or a function argument, all of which bind looser than `&`, so there is no live
precedence bug. All 8 uses are inside the `#ifdef Py_GIL_DISABLED` block that defines it — verified;
`TYPE_LOCK` is never referenced in the GIL build, where it has no definition.

**Fix.** Either a `static inline PyMutex *type_lock(void)` or lowercase the name to signal it is an
expression, not a constant.

---

### [POLICY] Lowercase function-shaped macros shadowing static functions — `Objects/typeobject.c:192-196`

```c
#define types_world_is_stopped() 1
#define types_stop_world()
#define types_start_world()
#define type_lock_prevent_release()
#define type_lock_allow_release()
```

In the GIL build these five names are macros; in the free-threaded build they are `static`
functions (`:90`, `:124`, `:133`, `:144`, `:166`). The lowercase naming is the deliberate choice
that lets one call syntax serve both builds — but it defeats the convention that ALL-CAPS means
"macro, expansion may surprise you", and `types_world_is_stopped()` changes *return semantics*
(a runtime `bool` versus the literal `1`). This is the single highest-cost item in Part 2; see
BLIND-01.

---

### [POLICY] `NEXT_VERSION_TAG(interp)` yields a modifiable lvalue and is used with `++` — `Objects/typeobject.c:60`

```c
#define NEXT_VERSION_TAG(interp) \
    (interp)->types.next_version_tag
```

Parameter parenthesised, whole expansion **not** parenthesised — safe only because `->` binds
tighter than everything. The design intent is an lvalue: `:1451` reads
`set_version_unlocked(type, NEXT_VERSION_TAG(interp)++)`. A macro that silently permits mutation of
interpreter-global state is a readability hazard and is invisible to any search for writes to
`next_version_tag`.

---

## ACCEPTABLE — checked, no action

| Item | Where | Why acceptable |
|---|---|---|
| Header guards | all 4 in-scope headers | `#ifndef Py_INTERNAL_*_H` / `#define` / closing `#endif` with matching comment, plus a `#ifndef Py_BUILD_CORE` `#error`. **4/4 correct.** |
| `MCACHE_HASH(version, name_hash)` | `typeobject.c:49` | Both parameters cast-and-parenthesised, whole expansion parenthesised. Textbook. |
| `TYPE_IS_REVEALED(tp)` | `typeobject.c:102` | `((((PyObject *)(tp))->ob_flags & …) != 0)` — fully parenthesised. The 32-bit `0` variant (`:104`) is a build-configuration issue (BLIND-12), not a hygiene one. |
| `CHECK(expr)` | `typeobject.c:868` | `do { … } while (0)`, `(expr)` parenthesised, `#undef`'d at `:896`. **The exemplar in this file.** |
| `PyTypeObject_CAST` / `superobject_CAST` / `PyBufferWrapper_CAST` | `:200`, `:12543`, `:11252` | `((T *)(op))` — correct. |
| `_PyCFunction_TrampolineCall` | `pycore_object.h:996` | `(meth)((self), (args))` — every token parenthesised. |
| `MANAGED_DICT_OFFSET` / `MANAGED_WEAKREF_OFFSET` | `pycore_object.h:922-926` | `(((Py_ssize_t)sizeof(PyObject *))*-1)` — parenthesised, and the two build variants differ only in the multiplier, as intended. |
| `_PyReftracerTrack` | `pycore_object.h:111` | `do { … } while (0)`, arguments parenthesised. |
| `Py_BEGIN_CRITICAL_SECTION*` / `Py_END_CRITICAL_SECTION*` | `pycore_critical_section.h:255-290` | Deliberately brace-unbalanced (BEGIN opens `{`, END closes `}`). This is CPython's documented scoped-lock idiom and the reason `scan_lock_discipline` exists. Not a defect. |
| `SLOT0`/`SLOT1`/`SLOT1BINFULL` | `typeobject.c:10524-10614` | Generate whole function bodies; `Py_TYPE(other)` is evaluated 5× inside `SLOT1BINFULL`, but on the *generated function's own parameters*, so no caller-visible multiple evaluation. |
| `pycore_typeobject.h` constants | `:18-35` | `_Py_TYPE_BASE_VERSION_TAG (2<<16)` and `_Py_MAX_GLOBAL_TYPE_VERSION_TAG (… - 1)` both parenthesised. |
| `#if _Py_ATTR_CACHE_UNUSED < MAX_VERSIONS_PER_CLASS` `#error` | `typeobject.c:1390-1392` | A compile-time cross-header invariant check. Good practice; the report notes it as the pattern the `SIGNATURE_END_MARKER_LENGTH` finding is missing. |

## Macro scope — `#undef` discipline

Three macros are correctly scoped: `CHECK` (868→896), `COPYVAL` (8761→8768), `NO_SPEC` (5305→5433).

**Everything else leaks to EOF.** The `COPY*` family (`SLOTDEFINED`, `COPYSLOT`, `COPYNUM`,
`COPYSEQ`, `COPYMAP`, `COPYBUF`, `COPYASYNC`) is `#undef`'d **before** it is defined
(`:8826-8831`, a defensive re-definition guard) but never after `inherit_slots` ends at `:9015`,
so seven macros carrying `type`/`base`/`basebase` captures remain live for the following ~4,050
lines. The 14-name `TPSLOT`/`ETSLOT`/… family (`:11547-11583`) has the same pattern and leaks the
final 1,485 lines. `COPYASYNC`, `BINSLOTNOTINFIX` and `RBINSLOTNOTINFIX` are missing from their
respective defensive `#undef` lists entirely.

No live collision exists — nothing later in the file uses those names. Classification: **CONSIDER**,
one line each, and the fix makes the capture-of-caller-locals hazard (`SLOTDEFINED`) structurally
impossible to reach from outside `inherit_slots`.

---

# PART 2 — THE MACRO-BLINDNESS INVENTORY

> **This section is the deliverable for the synthesis step.** Every row is a file-local macro or
> trivial wrapper in `Objects/typeobject.c` (or an in-scope header) that hides a token a static
> scanner keys on. Counts are measured, not estimated. Rows are ordered by measured recall cost —
> see Part 3 for the ranking rationale and the resolver-build order.

## 2.1 What each scanner actually resolved on this file (measured)

| Scanner | Denominator reported | Reality | Verdict |
|---|---|---|---|
| `scan_lock_discipline` | `vocabulary_resolved=66`, `critical_section_functions=23`, `mutex_functions=3` | Resolves `BEGIN_TYPE_LOCK`/`END_TYPE_LOCK`/`BEGIN_TYPE_DICT_LOCK`/`END_TYPE_DICT_LOCK` correctly via `resolve_local_lock_macros`. | **Working.** 0 findings over a real denominator. |
| `scan_stw_safety` | `stw_functions=3` | Keys on the literal token `_PyEval_StopTheWorld`, which appears in exactly 3 functions. There are **11** STW regions in 10 functions. | **82 % blind.** See BLIND-01. |
| `scan_ft_races` | `lock_held_functions=0` | Its `_LOCK_TOKENS` are `Py_BEGIN_CRITICAL_SECTION` / `PyMutex_Lock` / `_PyCriticalSection` plus the `*_lock_held` naming convention. **`scan_ft_races.py` does not import `resolve_local_lock_macros` — only `scan_lock_discipline.py` defines it.** | **Total blind spot, net-new.** 21 `BEGIN_TYPE_LOCK()` regions are invisible; every guarded read in this file looks unguarded. |
| `scan_refcounts` | `borrowed_field_accessors=4`, `borrowed_accessor_load_sites=45` | Keys correctly on the four `lookup_tp_*` accessors. Cannot see the same shape when the borrowed pointer is a plain `->tp_*` field read. | **Working, with a 12-site inverse gap.** See BLIND-03. |
| `tree_sitter_utils.extract_functions` | `functions_analyzed=459` | `_FUNCTION_MACROS` covers `SLOT0`/`SLOT1`/`SLOT1BIN`/`SLOT1BINFULL`, so 34 generated functions **are** seen. `RICHCMP_WRAPPER` is not, because its name is token-pasted (`richcmp_##NAME`). | **6 functions invisible, net-new.** See BLIND-02. |
| `scan_gil_usage` | `vocabulary_resolved=0`, `vocabulary_tokens_seen=4` | `grep -c 'ALLOW_THREADS\|PyGILState_'` on this file is 0. | **Structural zero, not a blind spot.** This file uses stop-the-world, never `ALLOW_THREADS`. Report "N/A", never "clean". |

## 2.2 The inventory

| ID | Macro / wrapper | Line | Expands to | Blinds | Hidden call sites |
|---|---|---|---|---|---|
| **BLIND-01** | `types_stop_world()` / `types_start_world()` | 124 / 133 (FT fn); 193 / 194 (GIL empty `#define`) | `_PyEval_StopTheWorld(interp)` / `_PyEval_StartTheWorld(interp)` | `scan_stw_safety` (token match on `_PyEval_StopTheWorld`) | **9 of 11** STW regions, in **8 functions**: `set_tp_mro` 669, `type_set_abstractmethods` 1745, `type_set_bases_unlocked` 1939 **and** 2005, `apply_type_slot_updates` 3923, `_PyType_SetFlags` 6457, `_PyType_SetFlagsRecursive` 6522, `object_set_class` 7823, `PyType_Freeze` 12523. Only `type_set_name` 1572 and `type_set_qualname` 1598 call `_PyEval_StopTheWorld` raw. |
| **BLIND-02** | `RICHCMP_WRAPPER(NAME, OP)` | 10265 | a whole `static PyObject *richcmp_##NAME(…)` function definition | **every function-level scanner** — the functions do not exist in `extract_functions()`'s output | **6** (`richcmp_lt/le/eq/ne/gt/ge`, invocations at 10272-10277). Verified by direct probe: `'richcmp_' prefix: 0 seen`. Generalises: `UNSIGNED_INT_CONVERTER` (`longobject.c:1812`, 9 fns) and `COMPLEX_BINOP` (`complexobject.c:696`, 4 fns) have the identical token-paste shape → **19 functions tree-wide**. |
| **BLIND-03** | `lookup_tp_dict` / `lookup_tp_bases` / `lookup_tp_mro` / `lookup_tp_subclasses` | 527 / 580 / 645 / 732 | branch on `_Py_TPFLAGS_STATIC_BUILTIN`, then the field read | `scan_refcounts` **keys on these** (`borrowed_field_accessors=4`) — the gap is the **inverse**: equivalent plain field reads bypass the accessor and are not counted | Accessor sites (seen): `lookup_tp_dict` 31, `lookup_tp_mro` 26, `lookup_tp_bases` 15, `lookup_tp_subclasses` 11. **Bypasses (unseen): 12** — `tp_dict` at 4960, 6812, 6818, 6820(write), 7020, 7211; `tp_mro` at 2931, 6037, 7022, 7213, 7273; `tp_bases` at 7021, 7214. Detail in §2.3. |
| **BLIND-04** | `BEGIN_TYPE_LOCK()` / `END_TYPE_LOCK()` / `BEGIN_TYPE_DICT_LOCK(d)` / `END_TYPE_DICT_LOCK()` | 79 / 80 / 82 / 85 (FT); 184-187 (GIL empty) | `Py_BEGIN_CRITICAL_SECTION_MUTEX(TYPE_LOCK)` etc. | **`scan_ft_races` only** — `scan_lock_discipline` resolves them | **44** (21 + 21 + 1 + 1). Resolved by `scan_lock_discipline`; **entirely invisible to `scan_ft_races`**, which reports `lock_held_functions=0` on a file with 23 critical-section functions. |
| **BLIND-05** | `type_lock_prevent_release()` / `type_lock_allow_release()` | 144 / 166 (FT fn); 195 / 196 (GIL empty) | splices `TYPE_LOCK` out of `tstate->critical_section`'s mutex list in place | **every** "is `TYPE_LOCK` held here" analysis — no scanner models it | **5 correctly-paired spans**: 668/674, 1938/1943, 2004/2009, 3922/3926, 6521/6525. Between a `prevent` and its `allow`, `TYPE_LOCK` is *held but not suspendable* — a state no tool represents. |
| **BLIND-06** | `type_set_flags` / `type_set_flags_with_mask` / `type_add_flags` / `type_clear_flags` | 455 / 462 / 470 / 476 | `tp->tp_flags = …` | any search for writes to `tp_flags`; also hides `ASSERT_WORLD_STOPPED_OR_NEW_TYPE` | **25 mutation sites** behind wrappers (`type_add_flags` ×17, `type_clear_flags` ×5, `type_set_flags` ×2 direct, `_with_mask` ×1) vs **exactly one** textual `tp->tp_flags = …` at 458. Plus one **bypass**: `_PyType_SetFlags` writes `self->tp_flags` inline at **6459** — see §2.4. |
| **BLIND-07** | `FT_ATOMIC_*` family (`pycore_pyatomic_ft_wrappers.h`) | 32-190 | `_Py_atomic_*` on FT; **bare identity** in the GIL build (`#define FT_ATOMIC_LOAD_UINT_RELAXED(value) value`, `:189`) | any atomic-vs-plain asymmetry check keyed on `_Py_atomic_*` | **10 sites in this file** (54, 1154, 1237, 1246, 1321, 6301, 6356, 6421, 6445, 6446). A token scan for `_Py_atomic_` in `typeobject.c` finds the seqlock/CAS uses but **none** of these 10. Concrete consequence: `tp_versions_used` is read atomically at 6301 and plainly at 1422 — the asymmetry is invisible because one side is behind a wrapper. |
| **BLIND-08** | `MCACHE_HASH_METHOD(type, name)` | 53 | contains `FT_ATOMIC_LOAD_UINT_RELAXED((type)->tp_version_tag)` | any scan for atomic reads of `tp_version_tag` | **1** — but it is at 6308, in `_PyType_LookupStackRefAndVersion`, the hottest attribute-read path in the interpreter. The only *visible* atomic read of `tp_version_tag` in that function is the acquire load at 6316. |
| **BLIND-09** | `ASSERT_TYPE_LOCK_HELD()` / `ASSERT_NEW_TYPE_OR_LOCKED(tp)` / `ASSERT_WORLD_STOPPED_OR_NEW_TYPE(tp)` | 108 / 115 / 112; empty at 118-120 and 188-191 | `_Py_CRITICAL_SECTION_ASSERT_MUTEX_LOCKED` / `assert(...)` / nothing | **precision, not recall** — `scan_lock_discipline` treats `ASSERT_TYPE_LOCK_HELD` as proof of a held lock | **22** (16 + 4 + 2). All three compile to **nothing** outside `Py_DEBUG` **and** are empty in the whole GIL build; `TYPE_IS_REVEALED` is additionally hard-`0` on 32-bit. An invariant enforced only by these fires on exactly one of four build configurations: FT + `Py_DEBUG` + 64-bit. |
| **BLIND-10** | `COPYSLOT` / `COPYNUM` / `COPYSEQ` / `COPYMAP` / `COPYBUF` / `COPYASYNC` / `COPYVAL` (+ `SLOTDEFINED`) | 8837, 8840-8844, 8761, 8833 | `if (!type->SLOT && …) type->SLOT = base->SLOT` | any scan for writes to type slots, and for the NULL guard on `basebase` | **69 expansions**, each containing one branch and one slot write, inside `inherit_slots`. A slot-write scan of that function finds **0**. The sole NULL guard on `basebase` lives inside `SLOTDEFINED`. |
| **BLIND-11** | `TPSLOT`/`FLSLOT`/`ETSLOT`/`BUFSLOT`/`AMSLOT`/`SQSLOT`/`MPSLOT`/`NBSLOT`/`UNSLOT`/`IBSLOT`/`BINSLOT`/`RBINSLOT`/`BINSLOTNOTINFIX`/`RBINSLOTNOTINFIX` | 11547-11583 | `pytype_slotdef` initialisers incl. `offsetof(...)` and `.name_strobj = &_Py_ID(NAME)` | any dunder→slot mapping built by reading `slotdefs[]` | **105 table entries.** The dunder name reaches the table only via `#NAME` stringification and `&_Py_ID(NAME)`; neither is a literal in the source. |
| **BLIND-12** | `TYPE_IS_REVEALED(tp)` | 102 (64-bit FT), 104 (32-bit → `0`), 189 (GIL → `0`) | flag test, or the constant `0` | branch-reachability analysis | **2 direct uses** (inside `ASSERT_NEW_TYPE_OR_LOCKED` and `ASSERT_WORLD_STOPPED_OR_NEW_TYPE`), transitively **6**. Two of the three definitions make the guarded branch statically dead. |
| **BLIND-13** | `managed_static_type_index_{is_set,get,set,clear}` + the `_Py_TPFLAGS_STATIC_BUILTIN` arm of `lookup_tp_subclasses` | 232-257, 732 | `tp_subclasses` used as a **1-based integer index**, not a `PyObject *` | any pointer-typed reasoning about `tp_subclasses` — refcount, NULL-safety, traverse | **5 sites** (234, 243, 251, 257, 740). `self->tp_subclasses = (PyObject *)(index + 1)` at 251 is a small integer cast to a pointer. A refcount scanner that treats `tp_subclasses` as an owned/borrowed `PyObject *` is wrong on every static builtin type. |
| **BLIND-14** | `_Py_ClearImmortal(op)` (`pycore_object.h:225`) | 225 | `…; op = NULL;` — the write is **inside** the macro | any search for writes to `tp_bases` / `tp_mro` | **2 in this file** (634 → `self->tp_bases = NULL`, 689 → `self->tp_mro = NULL`). These are the only two writes to those fields outside `set_tp_bases`/`set_tp_mro`. |
| **BLIND-15** | `NEXT_VERSION_TAG(interp)` | 60 | `(interp)->types.next_version_tag` — a modifiable lvalue | any search for writes to `interp->types.next_version_tag` | **1 write** (`NEXT_VERSION_TAG(interp)++` at 1451) + 1 read (1447). The post-increment on interpreter-global version state is invisible. |
| **BLIND-16** | `OBJECT_STAT_INC_COND(name, cond)` (`pycore_stats.h:23` / `:78`) | 78 (non-stats) | `((void)0)` — **the `cond` argument is discarded unevaluated** | call-graph and call-count analysis | **7 sites** in R19 (6220, 6319, 6320, 6341, 6342, 6347, 6348), whose `cond` arguments contain **6 calls to `is_dunder_name(name)`** that exist only under `--enable-pystats`. A call-graph built from source over-counts `is_dunder_name` by 6 for a default build. |
| **BLIND-17** | `PyTypeObject_CAST(op)` / `superobject_CAST(op)` / `PyBufferWrapper_CAST(op)` | 200 / 12543 / 11252 | `((T *)(op))` | type-based reasoning on `self` in slot functions | **38** (29 + 6 + 3). Benign individually; collectively they mean the concrete type of `self` is one hop away at every slot entry point. |
| **BLIND-18** | `#define slot_mp_length slot_sq_length` | 10713 | a function-name alias | any "find the definition of `slot_mp_length`" query | **1**. `slot_mp_length` is referenced from `slotdefs[]` but has no definition anywhere; it resolves only through this object-like macro. |

## 2.3 BLIND-03 detail — the accessor bypasses, and which ones actually differ

The four `lookup_tp_*` accessors are **not equivalent** to a plain field read. `lookup_tp_dict`
(527) and `lookup_tp_subclasses` (732) branch on `_Py_TPFLAGS_STATIC_BUILTIN` and read
per-interpreter state instead. `lookup_tp_bases` (580) and `lookup_tp_mro` (645) are pure aliases
(`return self->tp_bases;`). So the twelve bypasses split into two classes:

**Semantically different (accessor branch skipped):**

| Line | Function | Expression | Reachable with a static builtin? |
|---|---|---|---|
| 4960 | `type_new_impl` | `_PyDict_HasOnlyStringKeys(type->tp_dict)` | No — `type` is a freshly built heap type. |
| 6812 / 6818 | `type_setattro` | `PyObject *dict = type->tp_dict;` (double-checked) | **No, but only by a non-local invariant**: `type_setattro` rejects `Py_TPFLAGS_IMMUTABLETYPE` at **6754**, 58 lines earlier, and every `_Py_TPFLAGS_STATIC_BUILTIN` type is immutable. |
| 6820 | `type_setattro` | `dict = type->tp_dict = PyDict_New();` — a **plain write** bypassing `set_tp_dict` | same |
| 7020 / 7211 | `type_dealloc` / `type_traverse` | `Py_XDECREF(type->tp_dict)` / `Py_VISIT(type->tp_dict)` | No — heap types only. |

**Semantically identical (pure alias), but still invisible to the accessor-keyed rule:**
`tp_mro` at 2931, 6037, 7022, 7213, 7273; `tp_bases` at 7021, 7214.

**Named guarded twin, and a propagated bypass.** `PyType_IsSubtype` (**2931**) reads `a->tp_mro`
raw where 26 other sites use `lookup_tp_mro`. `PyType_GetBaseByToken_DuringGC` (**6037**) then
copies it *and says so*:

```c
    PyObject *mro = type->tp_mro;  // No lookup, following PyType_IsSubtype()
```

One deliberate bypass became the precedent for a second. Both are defensible (the GC-time caller
cannot take `TYPE_LOCK`), but the pattern is now self-propagating and each new instance is one more
site the accessor-keyed rule cannot reach.

## 2.4 BLIND-06 detail — the one real `tp_flags` bypass, with its guarded twin

```c
6453: _PyType_SetFlags(PyTypeObject *self, unsigned long mask, unsigned long flags)
6455:     unsigned long new_flags = (self->tp_flags & ~mask) | flags;
6456:     if (new_flags != self->tp_flags) {
6457:         types_stop_world();
6458:         // can't use new_flags here since they could be out-of-date
6459:         self->tp_flags = (self->tp_flags & ~mask) | flags;
6460:         types_start_world();
6461:     }
```

**Guarded twin:** `type_set_flags_with_mask` (462) computes the identical expression and carries
`ASSERT_WORLD_STOPPED_OR_NEW_TYPE(tp)`; `_PyType_SetFlagsRecursive` → `set_flags_recursive` (6491)
uses it. `_PyType_SetFlags` is the **only** site in the file that writes `tp_flags` without the
wrapper — and therefore without the assertion.

The bypass is **deliberate and currently correct**: the comment at 6458 says the pre-computed
`new_flags` is stale by the time the world stops, so the wrapper (which takes an already-computed
value) genuinely cannot be used here. The write *is* bracketed by STW at 6457/6460, so the
invariant holds. But:

- the debug assertion no longer covers it, so a future edit that moves the write outside the STW
  bracket is caught by nothing;
- because of **BLIND-01**, no scanner can see that 6459 is STW-protected either. The two blind
  spots compound: an unasserted, unwrapped, apparently-unprotected write to `tp_flags`.

**Proposed fix for the code:** a `type_set_flags_with_mask_world_stopped(tp, mask, flags)` that
re-reads `tp->tp_flags` inside the assertion-carrying wrapper. Classification: **CONSIDER**.

## 2.5 The compounding case — `object_set_class`

Worth calling out because it shows the blindness is not additive but multiplicative:

- `object_set_class` (7800) opens an STW region at **7823** — invisible to `scan_stw_safety`
  (BLIND-01).
- The risky work happens one frame down in `object_set_class_world_stopped` (7700) — and
  `scan_stw_safety`'s intra-file call graph expands *callees of a known STW function*, so with the
  opener unrecognised the callee is never reached either.
- Inside that callee, at **7778-7780**, sits `Py_BEGIN_CRITICAL_SECTION(dict)` /
  `Py_END_CRITICAL_SECTION()` — a **per-object lock acquired while the world is stopped**.
  `scan_lock_discipline` sees the critical section and correctly finds it balanced;
  `scan_stw_safety` cannot see that it runs under STW. **No scanner in the toolkit can currently
  form the sentence "this file takes a per-object lock with the world stopped."**

(Whether that is *unsafe* is the STW/lock agents' call — flagged here purely as the measured cost
of BLIND-01.)

---

# PART 3 — Ranking by recall cost, and the resolver build order

| Rank | ID | Measured cost | Build cost | Notes |
|---|---|---|---|---|
| **1** | BLIND-01 `types_stop_world` | 9 of 11 STW regions in 8 functions invisible → `scan_stw_safety` recall **18 %** on the highest-tier file in `Objects/`; compounds with the callee-expansion gap (§2.5) | Medium | **Build first.** Note the fix shape: `resolve_local_lock_macros` **cannot** solve this. In the FT build `types_stop_world` is a `static` *function*, not a `#define`; in the GIL build it is a `#define` with an **empty body**, which `resolve_local_lock_macros` explicitly skips (`if body:`). The resolver must learn **trivial static wrappers**: a `static void f(void)` whose body contains exactly one call to a vocabulary token. That generalises across `Objects/` and `Python/`. |
| **2** | BLIND-04 `BEGIN_TYPE_LOCK` × `scan_ft_races` | 44 hidden acquire/release sites; `lock_held_functions=0` on a file with 23 critical-section functions → every guarded read in the file looks unguarded to the race scanner | **Low** | **Cheapest high-value fix in the list.** `resolve_local_lock_macros` already exists and already works — it is just not shared. Hoist it from `scan_lock_discipline.py` into the common module and consume it from `scan_ft_races.py`. Net-new finding; not in the preflight include map. |
| **3** | BLIND-02 `RICHCMP_WRAPPER` | 6 functions invisible here, **19 tree-wide** (`+ longobject.c` 9, `complexobject.c` 4) | **Very low** | `tree_sitter_utils._FUNCTION_MACROS` is a `{name: arg_index}` dict and cannot express a token-pasted name. Widen the value to `(arg_index, prefix, suffix)` and add `"RICHCMP_WRAPPER": (0, b"richcmp_", b"")`, `"UNSIGNED_INT_CONVERTER": (0, b"_PyLong_", b"_Converter")`, `"COMPLEX_BINOP": (0, b"complex_", b"")`. One table edit + one format change. |
| **4** | BLIND-03 accessor bypasses | 12 plain-field reads invisible to `borrowed_field_deref_across_call`; 2 of them (2931, 6037) are a self-propagating pattern | Medium | Widening `scan_refcounts` to treat `X->field` (where `field` is a `PyObject *`/`PyTypeObject *` member with a known in-file `Py_XSETREF`/`Py_SETREF`/setter) as an accessor return is already proposed in the preflight map. This measurement supplies the denominator: **45 accessor sites seen, 12 bypasses unseen ⇒ current recall 79 %** on this shape in this file. |
| **5** | BLIND-07 `FT_ATOMIC_*` | 10 sites here; the family is used across all of `Objects/` and `Python/` | Low | A static alias table (`FT_ATOMIC_LOAD_* → atomic read`, `FT_ATOMIC_STORE_* → atomic write`) resolves it without any per-file work. Highest leverage-per-line in the list because it is tree-wide, not file-local. Enables the atomic-vs-plain asymmetry check to see `tp_versions_used` (atomic at 6301, plain at 1422). |
| **6** | BLIND-05 `type_lock_prevent_release` | 5 spans where "is `TYPE_LOCK` held" has an answer no tool represents | High | Requires modelling a *third* lock state (held-but-not-suspendable). Probably not worth a resolver; worth a **file-level annotation** so agents reading these 5 spans are told the state is unusual. |
| **7** | BLIND-06 `type_*_flags` wrappers | 25 hidden `tp_flags` writes + 1 bypass at 6459 | Low | Same trivial-static-wrapper machinery as rank 1 — build once, get both. |
| **8** | BLIND-09 assertion macros | 22 sites; **precision** risk, not recall — `scan_lock_discipline` currently trusts a no-op | Low | Do not remove the resolution; **annotate** it. Emit the build-configuration caveat alongside any finding whose triage rested on an `ASSERT_*` macro: enforced on FT + `Py_DEBUG` + 64-bit only. |
| **9** | BLIND-10 `COPY*` | 69 hidden slot writes + the only `basebase` NULL guard | Low | Purely a `scrub_macros` extension: expand `COPYSLOT`-shaped bodies. Low value on its own — `inherit_slots` is pass-1 territory — but cheap if the machinery from rank 3 lands. |
| **10** | BLIND-13 `tp_subclasses`-as-index | 5 sites where a `PyObject *` is an integer | Low | Not a resolver; a **suppression rule**. Any refcount/NULL finding on `tp_subclasses` in a `_Py_TPFLAGS_STATIC_BUILTIN` context is a false positive by construction. Add to the FP taxonomy. |
| 11 | BLIND-11 slotdef family | 105 table entries | Medium | Only matters if an agent wants a dunder→slot map from source. `Include/internal/pycore_slots.h` and the clinic output are cheaper sources. |
| 12 | BLIND-16 `OBJECT_STAT_INC_COND` | 6 phantom `is_dunder_name` calls | Very low | Add `OBJECT_STAT_INC*` / `STAT_INC` / `*_STAT_ADD` to `_ERASE_MACROS`. One-line. |
| 13 | BLIND-08 `MCACHE_HASH_METHOD` | 1 hidden relaxed atomic read | Very low | Falls out of rank 5 for free (the hidden token *is* an `FT_ATOMIC_*`). |
| 14 | BLIND-14 `_Py_ClearImmortal` | 2 hidden field writes | Very low | Add to a "macros that write through their argument" list. |
| 15 | BLIND-15 `NEXT_VERSION_TAG` | 1 hidden write | Very low | — |
| 16 | BLIND-17 `*_CAST` | 38 sites | Very low | Trivially resolvable; low value. |
| 17 | BLIND-12 `TYPE_IS_REVEALED` | 2 direct / 6 transitive | Very low | Documentation item, not a resolver. |
| 18 | BLIND-18 `slot_mp_length` | 1 | Very low | Documentation item. |

## Recommended order for the toolkit

1. **BLIND-04** (hoist `resolve_local_lock_macros` → `scan_ft_races`) — lowest cost, restores lock
   context to the whole FT race scanner on every CPython file that wraps the critical-section
   macros, not just this one.
2. **BLIND-02** (`_FUNCTION_MACROS` gains paste templates) — one table edit, +19 functions tree-wide.
3. **BLIND-01 + BLIND-06** (trivial-static-wrapper resolution) — one mechanism, two payoffs, and it
   is the single largest recall gap measured anywhere in this slice.
4. **BLIND-07** (`FT_ATOMIC_*` alias table) — tree-wide leverage.
5. **BLIND-03** (widen `borrowed_field_deref_across_call` past the accessor list).

---

# Clean negatives — checked, and why

- **Header guards: 4/4 correct.** `pycore_object.h`, `pycore_typeobject.h`,
  `pycore_critical_section.h`, `pycore_lock.h` each open with `#ifndef Py_INTERNAL_*_H` /
  `#define`, carry a `#ifndef Py_BUILD_CORE` `#error`, and close with a commented `#endif`.
  Denominator: 4 headers, 4 checked, 0 findings.
- **No live dangling-else.** All 22 `ASSERT_*` sites, all 69 `COPY*` sites, all 7 `NO_SPEC` sites
  and all 125 `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` sites are at statement position or inside
  braces. The findings above are latent shapes, not live bugs — stated as such.
- **No live multiple-evaluation defect.** Every macro that evaluates a parameter twice
  (`MCACHE_CACHEABLE_NAME`, `_Py_ClearImmortal`, `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED`) is
  called only with side-effect-free arguments — checked at all sites.
- **Arithmetic parenthesisation in `typeobject.c`: clean.** `MCACHE_HASH`, `MCACHE_HASH_METHOD`,
  `TYPE_IS_REVEALED`, `COLLECTION_FLAGS`, `MANAGED_*_OFFSET`, `_Py_TYPE_BASE_VERSION_TAG`,
  `_Py_MAX_GLOBAL_TYPE_VERSION_TAG` and all three `*_CAST` macros are fully parenthesised. The two
  unparenthesised-result macros (`NEXT_VERSION_TAG`, `_Py_DEC_REFTOTAL`) expand to `->`/`.`
  member accesses, where nothing binds tighter — safe by construction rather than by discipline,
  hence POLICY rather than FIX.
- **`FT_ATOMIC_*` misuse in this file: none.** All 10 uses pass a simple member access and consume
  the result in a context where the GIL build's bare-identity expansion cannot re-associate
  (checked individually at 54, 1154, 1237, 1246, 1321, 6301, 6356, 6421, 6445, 6446). The
  unparenthesised GIL-build definitions (`pycore_pyatomic_ft_wrappers.h:150, 158, 188-190`) are a
  latent hazard for *other* callers, not a defect here.
- **`TYPE_LOCK` in the GIL build: not a bug.** All 8 uses are inside the `#ifdef Py_GIL_DISABLED`
  block that defines it; verified by line numbers (79, 83, 109, 150, 155, 159, 173, 178). The
  remaining textual occurrences (3802, 3905, 3913-3920, 6519) are comments.
- **`basebase` uninitialised-read: none.** Declared without an initialiser at `:8824`, but all six
  `COPY*` runs are preceded by an assignment in the same block, and `SLOTDEFINED`'s
  `basebase == NULL ||` guards the only deref. Traced all six.

---

# Cross-references for the synthesis step

- **§2.4 (`_PyType_SetFlags` 6459)** is a live CONSIDER for the **lock/STW agent**, not just a
  hygiene note — the guarded twin is named and the reason the bypass exists is documented in the
  code itself.
- **§2.5 (`object_set_class` → `Py_BEGIN_CRITICAL_SECTION` under STW)** is a hand-off to the
  **stw-safety agent** with the note that it cannot currently reach the site on its own.
- **BLIND-03 §2.3** supplies the denominator (45 seen / 12 unseen) the preflight map's proposed
  `scan_refcounts` widening was missing.
- **BLIND-04 and BLIND-02 are net-new**: neither appears in `preflight/include_map.md` §4, and both
  were found by probing the toolkit's own code rather than by reading CPython.
