# macro-hygiene-reviewer — Objects/ sample (informed)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scope:** the 14-file `Objects/` sample + every header they take macros from (`Include/`, `Include/cpython/`, `Include/internal/`)
**Primary assignment:** inventory the brace-unbalanced macro family as the implementable fix for the confirmed chassis defect.

## Scanner volume

This agent has no scanner. All numbers below are measured this session with
`tree_sitter_c` 0.26.0 against the real headers, not recalled.

| deliverable | measured |
|---|---|
| `#define`s scanned in `Include/**/*.h` | 3,522 across 295 headers (288 excl. vendored mimalloc) |
| macros with a **non-zero brace delta** | **28 sites / 17 distinct names** (13 sites / 9 names excluding vendored mimalloc) |
| macros that **supply their own terminator** (`;` / `,` / `_Pragma`) | 72 sites / 48 distinct names |
| ERROR-node cut from the substitution table | 4,379 → 3,289 tree-wide (**25%**) |
| functions recovered for `extract_functions()` | 12,807 → 13,095 tree-wide (**+288**) |

---

## Deliverable 1 — the brace-delta macro inventory

This is the pre-parse substitution table. Verified against the headers at
`4f3be1b5777`; every row was read, not recalled.

### 1a. Class BRACE — replacement list opens or closes a scope (delta ≠ 0)

`Δ` is the brace delta of the replacement list. `argfrm` = whether the use site
carries a `(...)` argument list that must be consumed along with the identifier.

| macro | Δ | argfrm | defining header:line | actual expansion |
|---|---|---|---|---|
| `Py_BEGIN_ALLOW_THREADS` | **+1** | no | `Include/ceval.h:119` | `{ PyThreadState *_save; _save = PyEval_SaveThread();` |
| `Py_END_ALLOW_THREADS` | **−1** | no | `Include/ceval.h:124` | `PyEval_RestoreThread(_save); }` |
| `Py_BEGIN_CRITICAL_SECTION(op)` | **+1** | yes | `Include/critical_section.h:74` (stable ABI) | `{ PyCriticalSection _py_cs; PyCriticalSection_Begin(&_py_cs, _PyObject_CAST(op))` |
| `Py_BEGIN_CRITICAL_SECTION(op)` | **+1** | yes | `Include/internal/pycore_critical_section.h:255` (FT core) | `{ PyCriticalSection _py_cs; PyThreadState *_cs_tstate = _PyThreadState_GET(); _PyCriticalSection_Begin(_cs_tstate, &_py_cs, _PyObject_CAST(op))` |
| `Py_BEGIN_CRITICAL_SECTION(op)` | **+1** | yes | `Include/cpython/critical_section.h:50` (GIL build) | `{` — bare, args discarded |
| `Py_END_CRITICAL_SECTION()` | **−1** | yes | `critical_section.h:79` / `pycore_critical_section.h:269` / `cpython/critical_section.h:54` | `PyCriticalSection_End(&_py_cs); }` / `}` |
| `Py_BEGIN_CRITICAL_SECTION2(a,b)` | **+1** | yes | `critical_section.h:83`, `pycore_critical_section.h:274`, `cpython/critical_section.h:56` | `{ PyCriticalSection2 _py_cs2; PyCriticalSection2_Begin(&_py_cs2, …)` |
| `Py_END_CRITICAL_SECTION2()` | **−1** | yes | `critical_section.h:88`, `pycore_critical_section.h:288`, `cpython/critical_section.h:60` | `PyCriticalSection2_End(&_py_cs2); }` |
| `Py_BEGIN_CRITICAL_SECTION_MUTEX(m)` | **+1** | yes | `cpython/critical_section.h:52` & `:65`, `pycore_critical_section.h:262` | `{ PyCriticalSection _py_cs; …_BeginMutex(&_py_cs, m)` |
| `Py_BEGIN_CRITICAL_SECTION2_MUTEX(m1,m2)` | **+1** | yes | `cpython/critical_section.h:58` & `:70`, `pycore_critical_section.h:281` | `{ PyCriticalSection2 _py_cs2; …_BeginMutex(&_py_cs2, m1, m2)` |
| `Py_BEGIN_CRITICAL_SECTION_SEQUENCE_FAST(o)` | **+1** | yes | `Include/internal/pycore_critical_section.h:29` (FT) and `:77` (GIL) | FT: `{ PyObject *_orig_seq = …; const bool _should_lock_cs = PyList_CheckExact(…); PyCriticalSection _cs; if (_should_lock_cs) { … }` · GIL: `{` |
| `Py_END_CRITICAL_SECTION_SEQUENCE_FAST()` | **−1** | yes | `pycore_critical_section.h:38` (FT) and `:78` (GIL) | FT: `if (_should_lock_cs) { … } }` · GIL: `}` |
| `_Py_BEGIN_SUPPRESS_IPH` | **+1** | no | `Include/internal/pycore_fileutils.h:292` | `{ _invalid_parameter_handler _Py_old_handler = _set_thread_local_invalid_parameter_handler(…);` (MSVC branch only; empty elsewhere) |
| `_Py_END_SUPPRESS_IPH` | **−1** | no | `Include/internal/pycore_fileutils.h:295` | `_set_thread_local_invalid_parameter_handler(_Py_old_handler); }` |
| `mi_commit_mask_foreach(...)` | **+1** | yes | `Include/internal/mimalloc/mimalloc/internal.h:717` | vendored mimalloc; `{ … for(…) {` |
| `mi_commit_mask_foreach_end` | **−1** | no | `Include/internal/mimalloc/mimalloc/internal.h:721` | vendored mimalloc; `} }` |

**Correction to the assignment brief:** `Py_TRASHCAN_BEGIN(op, dealloc)` and
`Py_TRASHCAN_END` are **Δ = 0 on main** — both are defined *empty* at
`Include/cpython/object.h:446-447` under the comment
`/* For backwards compatibility with the old trashcan mechanism */`. The
trashcan was reimplemented in `_PyTrash_thread_deposit_object`; the macros are
now no-ops and are **not** a parse hazard. They must not go in the table.

Likewise there are **no** brace-unbalanced `_Py_atomic` helpers — `pyatomic_gcc.h`,
`pyatomic_std.h` and `pyatomic_msc.h` contain zero GCC statement-expressions
(`({ … })`) and zero unbalanced macros. That branch of the brief is a clean negative.

### 1b. Class PUNCT — Δ = 0 but the replacement list supplies its own `;` or `,`

These are equally fatal and were **not** in the brief. The use site omits the
terminator because the macro provides it, so the raw token stream juxtaposes two
declarations. `PyObject_HEAD` alone accounts for 24 of the sample's 48 ERROR nodes.

| macro | supplies | defining header:line | actual expansion |
|---|---|---|---|
| `PyObject_HEAD` | `;` | `Include/object.h:65` | `PyObject ob_base;` |
| `PyObject_VAR_HEAD` | `;` | `Include/object.h:108` | `PyVarObject ob_base;` |
| `PyObject_HEAD_INIT(type)` | `,` | `Include/object.h:78` / `:89` | `{ { _Py_STATIC_IMMORTAL_INITIAL_REFCNT }, (type) },` |
| `PyVarObject_HEAD_INIT(type,size)` | `,` | `Include/object.h:96` | `{ PyObject_HEAD_INIT(type) (size) },` |
| `PyException_HEAD` | `;` | `Include/cpython/pyerrors.h:8` | `PyObject_HEAD PyObject *dict; …;` |
| `_PyGenObject_HEAD(prefix)` | `;` | `Include/internal/pycore_interpframe_structs.h:59` | struct member block ending `;` |
| `_PyTZINFO_HEAD` | `;` | `Include/datetime.h:52` | `PyObject_HEAD Py_hash_t hashcode; …;` |
| `_PyDateTime_TIMEHEAD` | `;` | `Include/datetime.h:72` | `_PyTZINFO_HEAD unsigned char data[…];` |
| `_PyDateTime_DATETIMEHEAD` | `;` | `Include/datetime.h:100` | `_PyTZINFO_HEAD unsigned char data[…];` |
| `_Py_COMMON_FIELDS(prefix)` | `;` | `Include/cpython/funcobject.h:11` | member block ending `;` |
| `_ASDL_SEQ_HEAD` | `;` | `Include/internal/pycore_asdl.h:24` | `Py_ssize_t size; …;` |
| `PyABIInfo_VAR(name)` | `;` | `Include/modsupport.h:142` | declaration ending `;` |
| `STRUCT_FOR_ID` / `STRUCT_FOR_STR` | `;` | `Include/internal/pycore_global_strings.h:23,25` | `PyObject *_py_ ## NAME;` |
| `PY_DEF_EVENT(op)` | `,` | `cpython/code.h:235`, `cpython/dictobject.h:89`, `cpython/funcobject.h:141` | X-macro enumerator `PY_ ## op,` |
| `GC_GENERATION_INIT(th)` | `,` | `pycore_interp_structs.h:278,286` | `{ {…}, {…}, th, 0 },` |
| `DLOPENFLAGS_INIT` | `,` | `pycore_import.h:56` | `.dlopenflags = …,` |
| `EXTRA_CASES` | `;` | `pycore_opcode_metadata.h:2090` | generated `case N: …;` block |
| `RARE_EVENT_INC` / `RARE_EVENT_INTERP_INC` | `;` | `pycore_stats.h:97,107` | statement ending `;` |
| `_PyCode_DEF_UNIQUE_ID` / `_PyCode_DEF_THREAD_LOCAL_BYTECODE` | `;` | `cpython/code.h:37,40` | member declaration ending `;` |
| `_PyAtExit_LockCallbacks` / `_PyAtExit_UnlockCallbacks` | `;` | `pycore_atexit.h:15,16` | `PyMutex_Lock(&…);` |
| `PyStackRef_Borrow` | `;` | `pycore_stackref.h:529` | statement ending `;` |
| `import_curses` | `;` | `Include/py_curses.h:120` | `…;` |
| `Py_BLOCK_THREADS` / `Py_UNBLOCK_THREADS` | `;` | `Include/ceval.h:122,123` | `PyEval_RestoreThread(_save);` / `_save = PyEval_SaveThread();` |

### 1c. Class PRAGMA — expands to `_Pragma`/`__pragma`, no terminator

| macro | defining header:line | expansion |
|---|---|---|
| `_Py_COMP_DIAG_PUSH` | `Include/pyport.h:279 / 285 / 290` | `_Pragma("clang diagnostic push")` / `_Pragma("GCC diagnostic push")` / `__pragma(warning(push))` |
| `_Py_COMP_DIAG_IGNORE_DEPR_DECLS` | `Include/pyport.h:280 / 286 / 291` | `_Pragma("… ignored \"-Wdeprecated-declarations\"")` / `__pragma(warning(disable: 4996))` |
| `_Py_COMP_DIAG_POP` | `Include/pyport.h:282 / 288 / 292` | `_Pragma("… diagnostic pop")` / `__pragma(warning(pop))` |
| `Py_UNUSED(name)` | `Include/pymacro.h:234` | `__pragma`-adjacent MSVC form |
| `_GENERATE_DEBUG_SECTION_WINDOWS(name)` | `pycore_debug_offsets.h:29` | `__pragma(section(…))` + declaration |

**This class is the worst of the three**, because it fails *silently*. In
`Objects/object.c` the three-line `_Py_COMP_DIAG_PUSH / …IGNORE_DEPR_DECLS /
…POP` bracket at lines 1271-1274 inside `_PyObject_GetAttrId` produces **no
top-level ERROR node at all** — the parser instead nests the remaining **87
functions** (lines 1267-3521) *inside* `_PyObject_GetAttrId`'s body. Since
`extract_functions()` walks only top-level nodes, all 87 vanish with no
diagnostic. A `root_node.has_error` check does not catch this.

---

## Deliverable 2 — the Argument Clinic `*_METHODDEF` pattern

**Characterization.** Clinic emits `#define FOO_METHODDEF {"foo", …},` — the
replacement list *includes its own trailing comma*. The use site is therefore a
bare identifier on its own line inside a braced initializer list:

```c
static PyMethodDef mapp_methods[] = {
    DICT___CONTAINS___METHODDEF                        /* no comma */
    {"__getitem__", _PyDict_Subscript, METH_O | METH_COEXIST,
     getitem__doc__},
    DICT_GET_METHODDEF
    …
};
```

To tree-sitter-c that is `identifier` juxtaposed with `initializer_list` with no
separator. Measured rule (minimal cases, `tree_sitter_c` 0.26.0):

| shape | result |
|---|---|
| bare identifier element **followed by** another element | **ERROR** |
| bare identifier as the **last** element of the list | clean |
| two adjacent bare identifiers | **ERROR** |
| `*_GETSETDEF` in a `PyGetSetDef[]`, `*_METHODDEF` in `PyMethodDef[]` | identical behaviour |

So the trigger is precisely: *a bare identifier initializer element that is not
last*. Because Clinic tables almost always mix generated and hand-written
entries, essentially every Clinic-using method table contains one.

**Severity is combinatorial, not local.** In isolation tree-sitter recovers and
the following functions survive. The damage appears when a Clinic table sits in
the same file as the Class-BRACE macros: the METHODDEF error opens a recovery
region and the unbalanced `Py_BEGIN/END_CRITICAL_SECTION` text prevents the
parser from re-synchronising. In `Objects/dictobject.c` the result is a **single
ERROR node spanning lines 5268-8597 (3,330 lines)** that begins at
`DICT_COPY_METHODDEF` in `mapp_methods[]` and swallows the rest of the file —
109 of 296 functions, 51% of the file's bytes.

**Consequence for the fix (measured, and counter-intuitive):** substituting the
Clinic identifiers is *not* the right fix. Once the Class-BRACE macros are
substituted, tree-sitter recovers from the Clinic errors on its own. Adding a
Clinic substitution on top **reduces** the yield and introduces regressions:

| substitution set | functions recovered tree-wide | files regressed |
|---|---|---|
| BRACE + PUNCT + PRAGMA only | **+288** | 1 (`_cursesmodule.c`, −3) |
| … + Clinic → `{0},` | +217 | 2 (`dictobject.c` −115, `signalmodule.c` −23) |
| … + Clinic → erased | +114 | 2 (`dictobject.c` −115, `_cursesmodule.c` −3) |

---

## Deliverable 3 — routine macro-hygiene pass over the sample

The 14 sample files were enumerated for every `#define` (`genericaliasobject.c`
and `unionobject.c` define none). One defect is **FIX-grade and reproduces as a
live, pure-Python-reachable reference leak**; the rest are latent. Full list in
"Findings" below.

---

## Findings

### FIX

**0. `UNWRAP` — multi-statement macro with a hidden `return NULL`, not `do/while(0)`-wrapped → confirmed reference leak.**
`Objects/weakrefobject.c:544`.

```c
#define UNWRAP(o) \
        if (PyWeakref_CheckProxy(o)) { \
            o = _PyWeakref_GET_REF(o); \
            if (!proxy_check_ref(o)) { \
                return NULL; \
            } \
        } \
        else { \
            Py_INCREF(o); \
        }
```

This is the textbook category-4 defect (bare `if/else`, no `do { … } while (0)`)
with a real payoff: the macro also conceals a `return NULL` from its call sites.
Every caller that invokes it more than once returns out of the middle of an
acquire sequence, releasing nothing it has already acquired.

`Objects/weakrefobject.c:564` (`WRAP_BINARY`, which generates 25 slots):
```c
#define WRAP_BINARY(method, generic) \
    static PyObject * \
    method(PyObject *x, PyObject *y) { \
        UNWRAP(x); \
        UNWRAP(y); \
        PyObject* res = generic(x, y); \
        Py_DECREF(x); Py_DECREF(y); return res; \
    }
```
`UNWRAP(x)` leaves `x` holding a new strong reference (from `_PyWeakref_GET_REF`
or `Py_INCREF`). If `y` is a **dead** proxy, `UNWRAP(y)` returns `NULL` and `x`'s
reference is never released. Same shape at `:577` (`WRAP_TERNARY` — leaks
`proxy` if `v` fails; `proxy` and `v` if `w` fails) and hand-written at `:646-647`
(`proxy_richcompare`).

**Guarded twin:** `WRAP_UNARY` / `WRAP_METHOD`, which call `UNWRAP` exactly once
and so have nothing outstanding at the failure point. The bug is created purely
by the second and third invocations.

**Reachability:** pure Python, via any of the 25 binary / 2 ternary number slots
or a rich comparison, whenever the *second* operand is a dead proxy.

**Differential (verified independently on this tree's `./python`, 3.16.0a0; the
`UNWRAP`/`WRAP_*` region is byte-identical between the built commit `583cb03da83`
and HEAD — the only diff in the file is in `get_or_create_weakref` and
`weakref_methods`):**
```
proxy_add    refcount(a): before=2    after=1002  leaked=1000
richcompare  refcount(a): before=1002 after=2002  leaked=1000
CONTROL (both operands live)                      leaked=0
```
One permanently leaked strong reference to a **live** referent per call.

**Fix:** wrap the body in `do { … } while (0)` and make the failure action
explicit instead of a hidden `return` — e.g. `UNWRAP(o, ONERROR)` with
`WRAP_BINARY` passing `{ Py_DECREF(x); return NULL; }` for the second operand.
That closes the hygiene defect and the leak in one change.

**1. `_Py_COMP_DIAG_*` silently truncates `extract_functions()` output — chassis defect, no diagnostic.**
`Include/pyport.h:279-292` · used at `Objects/object.c:1271-1274` (28 use sites tree-wide).
A `_Pragma`-expanding macro used with no terminator makes tree-sitter nest 87
subsequent top-level functions inside `_PyObject_GetAttrId`. **No ERROR node is
produced at top level**, so this is invisible to any `has_error` guard. Every one
of the toolkit's 23 scanners silently loses 57% of `object.c`. Fix = add the
three names to the substitution table (erase them).

### CONSIDER

**2. `Py_BEGIN/END_CRITICAL_SECTION` is a *victim* as often as a cause — the brief's framing overstates it.**
Measured: a `Py_BEGIN_CRITICAL_SECTION(op); … Py_END_CRITICAL_SECTION();` pair in
isolation parses **cleanly** (both are valid call-expression statements) and the
enclosing function is extracted normally. Of the 23 `Py_BEGIN_CRITICAL_SECTION`
occurrences that fall outside an extracted function in `Objects/`, **18 are in
`dictobject.c` and lie inside the 3,330-line ERROR region opened by the Clinic
method table** — they are collateral, not causal. Correct statement of the defect:
the brace macros do not usually *start* a parse failure, but they reliably
*prevent recovery* from one.

**3. `#ifdef`-split `if` statements reproduce the brace-imbalance shape without any macro.**
`Objects/dictobject.c:6190-6196` in `dictiter_iternextitem`:
```c
#ifdef Py_GIL_DISABLED
    if (dictiter_iternext_threadsafe(d, self, &key, &value) == 0) {
#else
    if (dictiter_iternextitem_lock_held(d, self, &key, &value) == 0) {
#endif
```
Two `{` in the source text, one `}`. This alone opens a 2,417-line ERROR node.
A macro substitution table cannot fix this; it needs `#if` branch selection.
This is the residual 26 ERROR nodes still in `dictobject.c` after substitution.

**4. `exit:` label immediately before `Py_END_CRITICAL_SECTION()` breaks the naive substitution.**
`Objects/listobject.c:348-349` (`list_item_impl`), and the same idiom throughout the
FT code:
```c
exit:
    Py_END_CRITICAL_SECTION();
```
Substituting `}` yields `exit: }` — a label with no following statement, which is
not valid C and cost 62 functions in `listobject.c` in my first iteration. The
substitution must emit `;}` when the preceding non-whitespace character is `:`.
This is a required detail of the fix, not an optional refinement.

**5. `_PyTuple_HASH_XXROTATE(x)` — parameter not parenthesized, in a shift expression.**
`Include/internal/pycore_tuple.h:70` and `:75`:
```c
#define _PyTuple_HASH_XXROTATE(x) ((x << 31) | (x >> 33))  /* Rotate left 31 bits */
#define _PyTuple_HASH_XXROTATE(x) ((x << 13) | (x >> 19))  /* Rotate left 13 bits */
```
`<<` / `>>` bind tighter than `^`, `|`, `&`, comparisons and `?:`, so an argument
using any of those mis-associates *silently* — every operand is `Py_uhash_t`, so
it compiles clean and simply computes a different hash:
`_PyTuple_HASH_XXROTATE(acc ^ lane)` → `((acc ^ (lane << 31)) | (acc ^ (lane >> 33)))`.
`^` is not hypothetical here: the adjacent `_PyTuple_HASH_EMPTY` uses it and the
canonical upstream xxHash spelling is `XXROTATE(acc ^ lane)`. All four call sites
(`Objects/tupleobject.c:390`, `Objects/dictobject.c:8424`, `:8432`,
`Modules/_cursesmodule.c:1629`) currently pass the bare identifier `acc` — latent,
not live. Worth fixing because a silent hash miscomputation is near-undebuggable
and the macro is consumed from three translation units.
**Fix:** `(((x) << 31) | ((x) >> 33))` on both branches.

**6. `REAL_SIZE(op)` — parameter not parenthesized under a cast.**
`Objects/structseq.c:49`: `#define REAL_SIZE(op) get_real_size((PyObject *)op)`.
A cast binds tighter than any binary operator, so `REAL_SIZE(p + 1)` becomes
`get_real_size(((PyObject *)p) + 1)` — type-correct, no diagnostic, wrong object.
Its four siblings (`VISIBLE_SIZE`, `VISIBLE_SIZE_TP`, `REAL_SIZE_TP`,
`UNNAMED_FIELDS_TP`) all place the parameter in a call-argument position and are
safe; `REAL_SIZE` is the odd one out — that asymmetry is the guarded twin. All six
call sites pass a bare identifier. Latent. **Fix:** `get_real_size((PyObject *)(op))`.

**7. `FT_CLEAR_WEAKREFS` discards its second argument under `Py_GIL_DISABLED`.**
`Include/internal/pycore_weakref.h:32`. In the free-threaded branch
`weakref_list` is never substituted, so the expression is not evaluated and *not
even parsed* — a side-effecting or stale-named argument compiles clean under
`--disable-gil` and breaks only the default build. Called from four in-scope files
(`odictobject.c:1430`, `funcobject.c:1137`, `unionobject.c:25`,
`genericaliasobject.c:37`) plus 11 others; all 15 sites pass a plain `x->field`,
so this is a build-configuration asymmetry, not a live defect. Secondary: the
`weakref_list != NULL` test is unparenthesized.

### POLICY / ACCEPTABLE

- **`ONE` leaks into the whole translation unit** — `Objects/odictobject.c:557`, `#define ONE ((Py_ssize_t)1)`, never `#undef`'d, live for the remaining 1,828 lines of a 2,385-line file. Maximally collision-prone identifier. The same file demonstrates the correct pattern 160 lines later (`_odictnode_DEALLOC` defined at 719, `#undef`'d at 816). Fix: `#undef ONE` after its last use at line 608.
- **`odict_update` alias never `#undef`'d** — `Objects/odictobject.c:1212`, `#define odict_update mutablemapping_update`: a lowercase, function-shaped object-like macro live for 1,173 lines. Any future real `odict_update` is silently renamed. Only two uses (1393, 1572).
- **`OFF(x)` leaks across three unrelated structs** — `Objects/funcobject.c:615`, bound to `PyFunctionObject` but still live at `cm_memberlist` (1503) and `sm_memberlist` (1772), which spell `offsetof(classmethod, …)` out by hand. Verified this fails loudly (no shared member names), and no CPython file `#undef`s `OFF`, so this is tree-wide practice rather than a local deviation — POLICY only.
- **`SET_DICT_FROM_SIZE` captures caller-scope `v` and `dict`** — `Objects/structseq.c:486`. Correctly `do/while(0)`-wrapped and leak-free, but it clobbers a `v` that is not a parameter (and that exists at line 484 solely to serve the macro) and is never `#undef`'d.
- **`_odictnode_VALUE` is dead and its doc contradicts its definition** — `Objects/odictobject.c:523` defines `(node, od)`; the file's own API doc block at line 76 lists `(od, node)`. Zero call sites. Delete it or fix both.
- **`Py_BEGIN_ALLOW_THREADS` brace-splitting is deliberate and documented** — it exists so `_save` is scoped to the released region. Not a defect to fix upstream; purely a tooling concern. ACCEPTABLE.
- **`PyObject_HEAD` / `PyVarObject_HEAD_INIT` supplying their own `;` / `,`** — load-bearing CPython idiom that predates C99 and is relied on by every extension. ACCEPTABLE.
- **46 headers in `Include/cpython/` have no `#ifndef` guard** — by design, not a defect. Each is included exactly once from its parent (`Include/object.h` does `#define Py_CPYTHON_OBJECT_H` / `#include "cpython/object.h"` / `#undef Py_CPYTHON_OBJECT_H`) and several assert it (`#ifndef Py_CPYTHON_OBJECT_H # error "this header file must not be included directly"`). Clean negative — do not report.
- **`Py_TRASHCAN_BEGIN` / `Py_TRASHCAN_END` are empty no-ops on main** (`Include/cpython/object.h:446-447`). No hygiene issue, no parse hazard.
- **Naming:** all sample-local macros are ALL_CAPS except Clinic-generated ones. No collisions found. POLICY, no action.

---

## Classes bounded (clean negatives)

- **`_Py_atomic` helper macros:** `pyatomic.h`, `pyatomic_gcc.h`, `pyatomic_std.h`, `pyatomic_msc.h` contain **zero** unbalanced macros and zero `({ … })` statement-expressions. The brief's suspicion is unfounded.
- **`Objects/*.c` locally-defined macros:** across all 50 files, **zero** macros have a non-zero brace delta. The hazard is entirely header-borne plus Clinic-generated.
- **The `*_CAST` family is clean.** All twelve sample-local `*_CAST` macros resolve through `_Py_CAST(type, expr)` = `((type)(expr))` (`Include/pyport.h:37`) and are parenthesization-safe. The `assert(check(op)), _Py_CAST(…, op)` form does double-evaluate `op` in debug builds only; all ~50 call sites pass a bare slot parameter.
- **`_odictnode_KEY/_HASH/_PREV/_NEXT`** (`odictobject.c:518-526`) leave `node` unparenthesized, but `->` binds tightest, so a mis-associating argument is a compile error rather than silent corruption. All ~40 call sites pass a bare identifier or an already-parenthesized expansion. Latent only.
- **`_odictnode_DEALLOC`, `COPY_ATTR`, `CASE`** are correctly `do/while(0)`-wrapped *and* correctly `#undef`'d in scope — these are the in-file guarded twins for the `ONE` / `odict_update` / `OFF` findings above.
- **The 14-file sample is not affected by the chassis defect.** Measured: `extract_functions()` returns 505 functions across the sample both before and after substitution — **no function is lost in any of the 14 files**. ERROR nodes drop 48 → 9, but none of them were suppressing a function. **The findings of the other 11 agents on this sample are not compromised by this defect.** The defect bites `dictobject.c` (−109), `object.c` (−87) and `bytesobject.c` (−40), all outside the sample.

---

## Toolkit assessment

### Precision
No scanner exists for this agent, so there are no false positives to report. The
one precision-relevant result is negative and important: **the brief's claim that
"19 of 187 `Py_BEGIN_CRITICAL_SECTION` occurrences (10%) fall outside any
extracted function" reproduces (I measure 23/191 = 12% at `4f3be1b5777`) but the
causal attribution is wrong.** The critical-section macros are mostly collateral
damage from Clinic-triggered ERROR regions.

### Recall gaps
1. **The PUNCT class was missing from the brief entirely.** `PyObject_HEAD` is the single most frequent ERROR trigger in the sample (24 of 48 nodes). A brace-delta-only substitution table would have missed it.
2. **The PRAGMA class fails silently** and is the largest single-file loss measured (87 functions in `object.c`). No ERROR node at top level means no existing guard detects it.
3. **`#ifdef`-split braces** are a macro-free instance of the same shape and are the largest *residual* after substitution.

### Prompt issues
The brief's framing ("brace delta +1/−1/0") is a strict subset of the real
hazard — it would have missed the PUNCT class (`PyObject_HEAD`, the single most
frequent trigger) and the silent PRAGMA class entirely. Its `Py_TRASHCAN` and
`_Py_atomic` leads are both dead ends on main and cost time to disprove.

Conversely, the agent definition's routine checklist — which the brief explicitly
outranked — is what produced the run's only reproduced runtime bug (`UNWRAP`).
The "multi-statement macro without `do/while(0)`" rule is worth more than its
boilerplate reputation suggests, but only when paired with the follow-through the
checklist does *not* currently mandate: *read the call sites and ask what is
already owned at the hidden exit*. Recommend adding that as an explicit sub-step,
since the leak is invisible from the macro definition alone.

### Concrete tuning proposals (ranked)

1. **Add `scripts/preparse_scrub.py` implementing the table above**, called from `tree_sitter_utils.parse_file()` before `Parser.parse()`. Must be **length-preserving** (space-pad every replacement) so all downstream line/byte reporting stays valid against the original file. Validated: +288 functions, −25% ERROR nodes tree-wide, 1 minor regression. Required details: consume the `(...)` argument list along with the identifier; emit `;}` not `}` when the preceding non-whitespace char is `:`; **do not** substitute Clinic `*_METHODDEF` (it lowers the yield).
2. **Add a parse-quality gate to the JSON envelope** — emit `parse_quality: {error_nodes: N, functions_extracted: N, functions_in_tree: N, bytes_covered_pct: F}` per file. `functions_in_tree > functions_extracted` catches the silent PRAGMA class that `has_error` misses. Any file below ~90% byte coverage should be flagged as under-analyzed rather than reported clean.
3. **Fix `extract_functions()` to descend into `ERROR` nodes.** It currently walks top-level nodes only; a `function_definition` nested under an `ERROR` or inside another function's body is valid salvage. This recovers most of the loss with no substitution at all and is the cheapest single change.
4. **Add a `data/cpython_parse_hazard_macros.json`** carrying the three classes so the table is data-driven and refreshable per CPython release, matching the toolkit's existing data-file convention.
5. **Document in the agent prompt** that `Py_TRASHCAN_*` is a no-op on main and that `_Py_atomic` is clean, so future runs don't re-chase them.
