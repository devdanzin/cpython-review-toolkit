# macro-hygiene-reviewer — obj-mappings slice

Target: `/home/danzin/projects/cpython` @ `4f3be1b5777` (verified `git rev-parse HEAD`).
Scope: `Objects/dictobject.c` (8,597 lines), `Objects/setobject.c` (3,228 lines).
Headers read for definitions only: `Include/internal/pycore_dict.h`,
`Include/internal/pycore_setobject.h`, `Include/cpython/setobject.h`,
`Include/internal/pycore_pyatomic_ft_wrappers.h`,
`Include/internal/pycore_critical_section.h`, `Include/cpython/critical_section.h`,
`Include/pymacro.h`.

---

## 1. Denominator

**Macro definitions examined**

| file | `#define` directives | distinct names | function-like |
|---|---|---|---|
| `Objects/dictobject.c` | 63 | **42** | 58 |
| `Objects/setobject.c` | 14 | **12** | 4 |
| `Include/internal/pycore_dict.h` | 26 | **25** | 6 |
| `Include/internal/pycore_setobject.h` | 1 | 1 (header guard) | 0 |
| `Include/cpython/setobject.h` | 3 | 3 | 2 |
| `pycore_pyatomic_ft_wrappers.h` | 126 | 63 (dual-armed) | 125 |
| `pycore_critical_section.h` + `cpython/critical_section.h` | 27 | 11 (dual-armed) | 23 |

**79 macro definitions are defined *inside the slice*** (dictobject.c 63 + setobject.c 14
+ pycore_setobject.h 1 + pycore_dict.h's 26 minus overlap); **139 distinct macro names
were reviewed in total**, counting the cross-cutting FT-atomic and critical-section
families the two files consume.

**Macro use sites examined:** **875 macro-token occurrences** across **91 distinct macro
names**, enumerated mechanically over the two `.c` files
(`scratchpad/macro_audit.py`). Every one of the 91 was read at its definition; the
following were additionally read at **every** call site rather than sampled:

| macro | sites | macro | sites |
|---|---|---|---|
| `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` | 24 | `_PyAnyDict_CAST` | 9 |
| `ASSERT_DICT_LOCKED` | 21 | `LOCK_KEYS` / `UNLOCK_KEYS` | 6 + 6 |
| `ASSERT_CONSISTENT` | 20 | `PERTURB_SHIFT` | 11 |
| `_PySet_CAST` | 21 | `LINEAR_PROBES` | 6 |
| `USABLE_FRACTION` | 8 | `DK_SIZE` / `DK_MASK` | 6 + 5 |
| `IS_DICT_SHARED` / `SET_DICT_SHARED` | 12 + 4 | `LOAD_INDEX` / `STORE_INDEX` | 4 + 4 |
| `SET_IS_SHARED` / `SET_MARK_SHARED` | 6 + 3 | `LOCK_KEYS_IF_SPLIT` / `UNLOCK_..` | 2 + 2 |
| `GROWTH_RATE` | 1 | `frozendict_does_not_support` | 8 |

**Dual-definition (`#ifdef Py_GIL_DISABLED`) macros:** **21** — 19 in `dictobject.c`,
2 in `setobject.c` — plus the 11 dual-armed critical-section names and the 63
dual-armed `FT_ATOMIC_*` / `FT_MUTEX_*` names the slice consumes. Full expansion table
in §3.

**Sibling-hunt denominator (the §2.1 shape):** 89 raw "statement stranded after an
unconditional jump" candidates across both files; **88 are the brace-less-`if`-body
false positive**, 1 is real.

**Lock-macro pairing denominator:** 78 `Py_BEGIN/END_CRITICAL_SECTION*` pairs (41 + 6×2
in `dictobject.c`, 19 + 11×2 in `setobject.c`) — **0 arity mismatches**; 5 real
`LOCK_KEYS`/`UNLOCK_KEYS` regions and 2 `LOCK_KEYS_IF_SPLIT` regions — **0 escaping
`return`/`goto`**.

---

## 2. Findings

### 2.1 [CONSIDER] `Objects/dictobject.c:4380` (`dict_merge`) — `return` stranded inside a critical section by an incomplete free-threading conversion

```c
4376            if (status < 0) {
4377                Py_DECREF(iter);
4378                res = -1;
4379                goto slow_exit;
4380                return -1;          /* <-- dead */
4381            }
```

**Mechanism.** `Py_END_CRITICAL_SECTION()` is a *brace-emitting* macro pair, not a scoped
construct: under `Py_GIL_DISABLED` `Py_BEGIN_CRITICAL_SECTION(a)` expands to
`{ PyCriticalSection _py_cs; PyThreadState *_cs_tstate = …; _PyCriticalSection_Begin(…)`
and `Py_END_CRITICAL_SECTION()` to `_PyCriticalSection_End(_cs_tstate, &_py_cs); }`
(`pycore_critical_section.h:255-271`). A `return` between them exits the function without
ever calling `_PyCriticalSection_End`, leaving `a`'s per-object lock held forever. Under
the default build the same pair is *literally* `{` and `}`
(`cpython/critical_section.h:50-54`), so the identical source is harmless. Nothing in the
macro — no `__attribute__((cleanup))`, no diagnostic — protects against this.

**Why it is dead, and why that is the interesting part.** `git blame` shows the two
statements have different provenance:

```
92abb0124037 (Dino Viehland  2024-02-06 4378)   res = -1;
92abb0124037 (Dino Viehland  2024-02-06 4379)   goto slow_exit;
f95a1b3c53bd (Antoine Pitrou 2010-05-09 4380)   return -1;
```

`92abb012403` is **"gh-112075: Add critical sections for most dict APIs (#114508)"** —
the commit that wrapped `dict_merge`'s slow path in `Py_BEGIN_CRITICAL_SECTION(a)` and
rewrote every direct `return` into `res = …; goto slow_exit;` so the `END` would still
run. It converted six exits (`:4336`, `:4343`, `:4363`, `:4371`, `:4387` and this one)
and deleted the old `return` at five of them. **This is the sixth.** The residue is
unreachable today purely because the inserted `goto` precedes it.

**Guarded twin.** The other five converted exits in the *same function* — each is
`res = -1; goto slow_exit;` with no trailing `return`. Their guard addresses exactly this
threat model (lock release on an error exit), so citing them is sound.

**Why no compiler catches it.** `configure.ac:2700-2708` deliberately disables
`-Wunreachable-code` on GCC ("silently removed from the compiler") **and** on every debug
build. On the dominant toolchain nothing diagnoses the residue.

**Reproduction status.** No crash to reproduce — the statement is unreachable, verified by
reading the full `if` body (`:4376-4381`) and by the sibling-hunt scan finding no path to
it. **0/0 runs; static-confirmed dead code.** The *live* form of this shape (a `return`
between BEGIN and END) does not occur anywhere in either file — 78 pairs scanned, 0 hits.

**Fix.** Delete line 4380.

---

### 2.2 [CONSIDER] `Objects/dictobject.c:182-183` vs `:271-272` — `LOAD_INDEX` / `STORE_INDEX` carry a trailing `;` in the free-threaded arm only

```c
182 #define LOAD_INDEX(keys, size, idx)  _Py_atomic_load_int##size##_relaxed(&((const int##size##_t*)keys->dk_indices)[idx]);
183 #define STORE_INDEX(keys, size, idx, value) _Py_atomic_store_int##size##_relaxed(&(…)[idx], (int##size##_t)value);
                                                                                                                    ^ trailing ';'
271 #define LOAD_INDEX(keys, size, idx)  ((const int##size##_t*)(keys->dk_indices))[idx]
272 #define STORE_INDEX(keys, size, idx, value) ((int##size##_t*)(keys->dk_indices))[idx] = (int##size##_t)value
                                                                                                              ^ no ';'
```

**Mechanism.** `LOAD_INDEX` is an *expression* macro in the default build and a
*statement* macro in the free-threaded build. Any use in expression context compiles
under the GIL and is a hard syntax error under `Py_GIL_DISABLED`. Verified with a minimal
reduction (`scratchpad/loadidx.c`, gcc 15, `-std=c11`):

```
--- GIL arm ---  compiles clean
--- FT arm  ---  error: expected ')' before ';' token
                 note: in expansion of macro 'LOAD_INDEX'
```

**Live impact: none today.** All 8 call sites (`dictkeys_get_index:533/536/540/544`,
`dictkeys_set_index:561/565/569/574`) are statement-level `ix = LOAD_INDEX(…);` /
`STORE_INDEX(…);`, where the FT arm merely emits a stray null statement. This is
latent, not live — but it is *precisely* the "macro whose expansion differs between the
GIL and free-threaded builds" class, it makes the default build unable to validate the
free-threaded one, and the fix is deleting two characters.

**Guarded twin.** The 63 `FT_ATOMIC_*` macros in `pycore_pyatomic_ft_wrappers.h` — every
one of them is semicolon-free in both arms, so `FT_ATOMIC_LOAD_SSIZE_RELAXED` *is* usable
in expression context in both builds (and is so used, e.g. `dictobject.c:483`,
`setobject.c:1997`). `LOAD_INDEX`/`STORE_INDEX` are the two file-local hand-rolled
members of that family and the only two that break the invariant.

---

### 2.3 [CONSIDER] `Objects/dictobject.c:7603` — `#define CHECK(val) assert(val); if (!(val)) { return 0; }`

A two-statement macro with no `do { … } while (0)` wrapper **and** double evaluation of
`val`. `CHECK(x)` inside a brace-less `if` silently executes the `return 0;`
unconditionally.

**Live impact: none.** The whole block `:7602-7630` (`_PyObject_ManagedDictValidityCheck`)
is inside `#if 0`, so the preprocessor never sees the definition. Both of its two uses
(`:7609`, `:7621`, `:7625`) are inside braced blocks and pass side-effect-free
expressions, so even re-enabling `#if 1` would not misbehave *today* — but it would
double-evaluate `size == count` and re-read `tp->tp_flags`, and the next `CHECK` someone
adds under an unbraced `if` would silently return.

**Guarded twin — in the same file, same name, 6,900 lines above.** `dictobject.c:702`:

```c
#define CHECK(expr) \
    do { if (!(expr)) { _PyObject_ASSERT_FAILED_MSG(op, Py_STRINGIFY(expr)); } } while (0)
```

correctly `do`-wrapped, single-evaluation (the second `expr` is stringified, not
evaluated), and correctly `#undef`'d at `:798`. The `#if 0` twin is neither wrapped nor
`#undef`'d. That the file contains both spellings of the same idiom is the finding.

---

### 2.4 [CONSIDER] Five statement macros in `dictobject.c` are bare `if` statements, not `do { } while (0)`

| macro | line | free-threaded expansion | default expansion |
|---|---|---|---|
| `ASSERT_WORLD_STOPPED_OR_DICT_LOCKED(op)` | 171 | `if (!…world_stopped) { ASSERT_DICT_LOCKED(op); }` | *(empty)* |
| `ASSERT_WORLD_STOPPED_OR_OBJ_LOCKED(op)` | 175 | `if (!…world_stopped) { _Py_CS_ASSERT_OBJECT_LOCKED(op); }` | *(empty)* |
| `LOCK_KEYS_IF_SPLIT(keys, kind)` | 187 | `if (kind == DICT_KEYS_SPLIT) { LOCK_KEYS(keys); }` | *(empty)* |
| `UNLOCK_KEYS_IF_SPLIT(keys, kind)` | 192 | `if (kind == DICT_KEYS_SPLIT) { UNLOCK_KEYS(keys); }` | *(empty)* |
| `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(op)` | (`pycore_critical_section.h:64`) | `if (Py_REFCNT(op) != 1) { _PyCriticalSection_AssertHeldObj(…); }` — **FT + `Py_DEBUG` only** | *(empty)* |

**Mechanism, and the honest limit of it.** The textbook hazard is
`if (c) MACRO(a,b); else …`, where the FT arm's `if` swallows the `else`. I tested it
(`scratchpad/dangle.c`, gcc 15 `-std=c11`) and the outcome is **not** silent divergence:

```
=== GIL arm ===  compiles; prints "else-branch ran"   (macro is empty)
=== FT arm  ===  error: 'else' without a previous 'if'
```

The trailing `;` after the macro terminates the expanded `if`, orphaning the `else`, so
the free-threaded build fails loudly at compile time. **The failure mode is a build break
on one configuration, not a runtime behavioural difference.** That materially downgrades
this from the classic do-while finding. I report it because (a) PEP 7 / CPython house
style calls for `do { } while (0)` on statement macros, (b) a GIL-only contributor gets
no local signal, and (c) it costs one line each to fix. **No current call site is
affected** — 0/0 brace-less-`if` uses across all 5 macros' 32 combined call sites.

---

### 2.5 [ACCEPTABLE] `LOCK_KEYS_IF_SPLIT` / `UNLOCK_KEYS_IF_SPLIT` evaluate their predicate twice around the lock — verified safe

`_PyDict_CheckConsistency` (`dictobject.c:738` / `:794`):

```c
738        LOCK_KEYS_IF_SPLIT(keys, keys->dk_kind);
 …         /* 55 lines */
794        UNLOCK_KEYS_IF_SPLIT(keys, keys->dk_kind);
```

The lock predicate `keys->dk_kind` is a **separate load at the lock and at the unlock**.
If it could change in between, the FT build would acquire `keys->dk_mutex` and never
release it (or release one it never took). This is the exact "macro pair that is the
mechanism behind a leaked lock" shape the brief asks for, so I chased it to ground:

`dk_kind` is written at **exactly one place tree-wide** — `init_keys_object`,
`dictobject.c:836` — on a freshly allocated keys object before publication, and
`DICT_KEYS_SPLIT` keys are produced only through that same call
(`dictobject.c:7275`). Every other mention across `Objects/`, `Python/` and
`Include/internal/` is a read or a comparison. `keys` is also not reassigned between
`:709` and `:794`. **The predicate is immutable for the lifetime of the object, so the
pair is correct.**

Recorded as ACCEPTABLE rather than silence because the invariant is load-bearing and
asserted nowhere at these two lines; the robust spelling is to latch
`DictKeysKind kind = keys->dk_kind;` once, as `_Py_dict_lookup:1368/1385/1389` already
does (**that is the guarded twin, in the same file** — and its guard addresses this
threat model directly: it caches `kind` in a local precisely so the lock and unlock
cannot disagree).

---

### 2.6 [POLICY] Two lowercase macros with generic names, never `#undef`'d

- `Objects/setobject.c:64` — `#define dummy (&_dummy_struct)`. An **all-lowercase
  object-like macro** on one of the most generic identifiers in C, live for the remaining
  3,164 lines of the translation unit. 25 use sites, all `entry->key == dummy` / `!= dummy`
  comparisons plus one initializer (`:3189 PyObject *_PySet_Dummy = dummy;`). No local,
  parameter or member named `dummy` exists in the file, so there is no live capture — but
  any future local named `dummy` anywhere below line 64 is a confusing syntax error.
- `Objects/dictobject.c:2800` — `#define frozendict_does_not_support(WHAT)`, a lowercase
  *function-like* macro. All 8 call sites pass a string literal (required — `WHAT` is
  string-pasted), all are statement-level, and the body is a single `void` call, so there
  is no precedence or multiple-evaluation exposure. Naming only.

Both are PEP 7 §"Macros should be ALL_CAPS" deviations. Neither is a bug.

---

### 2.7 [ACCEPTABLE] The `*_CAST` family double-evaluates under `Py_DEBUG` — 32 sites checked, 0 exposed

```c
dictobject.c:325   #define _PyAnyDict_CAST(op)  (assert(PyAnyDict_Check(op)), _Py_CAST(PyDictObject*, op))
pycore_dict.h:454  #define _PyFrozenDictObject_CAST(op) (assert(PyFrozenDict_Check(op)), _Py_CAST(PyFrozenDictObject*, (op)))
cpython/setobject.h:61 #define _PySet_CAST(so)  (assert(PyAnySet_Check(so)), _Py_CAST(PySetObject*, so))
```

Each evaluates its argument **twice under `Py_DEBUG` and once under `NDEBUG`** — a
build-dependent evaluation count, which would make a side-effecting argument behave
differently between debug and release. I read all 32 call sites (9 `_PyAnyDict_CAST`,
2 `_PyFrozenDictObject_CAST`, 21 `_PySet_CAST`): **every one passes a bare identifier**
(`op`, `self`, `a`, `d`, `so`, `otherset`). No exposure. This is the CPython house idiom
and I am not proposing a change; the denominator is here so the class is measured rather
than assumed.

Note `_PyAnyDict_CAST` and `_PyFrozenDictObject_CAST` differ in a detail —
`(op)` is parenthesized in the second and not in the first — with no consequence, since
`_Py_CAST(T, x)` parenthesizes internally.

---

## 3. Dual-definition macro table (requested deliverable)

Every macro the slice uses whose definition depends on `Py_GIL_DISABLED`, with both
expansions. **Nothing in this table produces a dead status check** — the CPY-0099 shape
(`update_slot`'s `-1` discarded in the `#else` arm of an `#ifdef` whose other arm tests
it) has **no instance in either file**. Everything empty in one arm is empty *because the
GIL supplies the guarantee*, and every one is either an assertion or a
synchronisation primitive, never a status-producing call.

### `Objects/dictobject.c` (19 pairs)

| macro | `Py_GIL_DISABLED` | default (GIL) | dead in an arm? |
|---|---|---|---|
| `ASSERT_DICT_LOCKED(op)` | → static inline → `_Py_CS_ASSERT_OBJECT_LOCKED` | *empty* | assertion; intended |
| `ASSERT_WORLD_STOPPED_OR_DICT_LOCKED(op)` | `if (!world_stopped) { assert-locked }` | *empty* | assertion; intended |
| `ASSERT_WORLD_STOPPED_OR_OBJ_LOCKED(op)` | `if (!world_stopped) { assert-locked }` | *empty* | assertion; intended |
| `ASSERT_KEYS_LOCKED(keys)` | `assert(PyMutex_IsLocked(&keys->dk_mutex))` | *empty* | assertion; intended |
| `LOCK_KEYS(keys)` | `PyMutex_LockFlags(&keys->dk_mutex, _Py_LOCK_DONT_DETACH)` | *empty* | lock; intended |
| `UNLOCK_KEYS(keys)` | `PyMutex_Unlock(&keys->dk_mutex)` | *empty* | lock; intended |
| `LOCK_KEYS_IF_SPLIT(keys, kind)` | `if (kind==SPLIT) { LOCK_KEYS(keys); }` | *empty* | lock; §2.4/§2.5 |
| `UNLOCK_KEYS_IF_SPLIT(keys, kind)` | `if (kind==SPLIT) { UNLOCK_KEYS(keys); }` | *empty* | lock; §2.4/§2.5 |
| `INCREF_KEYS(dk)` | `_Py_atomic_add_ssize(&dk->dk_refcnt, 1)` → **prev value** | `dk->dk_refcnt++` → **prev value** | consistent ✓ |
| `DECREF_KEYS(dk)` | `_Py_atomic_add_ssize(&dk->dk_refcnt, -1)` → **prev value** | `dk->dk_refcnt--` → **prev value** | consistent ✓ (`:504 if (DECREF_KEYS(dk) == 1)` reads correctly in both) |
| `INCREF_KEYS_FT(dk)` | `dictkeys_incref(dk)` | *empty* | keys pinned only under FT; intended (`:1384`) |
| `DECREF_KEYS_FT(dk, shared)` | `dictkeys_decref(dk, shared)` | *empty* | ditto (`:1390`) |
| `LOAD_KEYS_NENTRIES(keys)` | `_Py_atomic_load_ssize_relaxed(&keys->dk_nentries)` | `keys->dk_nentries` | consistent ✓ |
| `LOAD_SHARED_KEY(key)` | `_Py_atomic_load_ptr_acquire(&key)` | `key` | consistent ✓ |
| `STORE_SHARED_KEY(key, value)` | `_Py_atomic_store_ptr_release(&key, value)` | `key = value` | consistent ✓ |
| `LOAD_INDEX(keys, size, idx)` | atomic load **+ trailing `;`** | plain subscript, no `;` | **asymmetric — §2.2** |
| `STORE_INDEX(keys, size, idx, value)` | atomic store **+ trailing `;`** | plain assign, no `;` | **asymmetric — §2.2** |
| `IS_DICT_SHARED(mp)` | `_PyObject_GC_IS_SHARED(mp)` | `(false)` | 12 sites, all feeding a `use_qsbr` parameter; QSBR is FT-only ✓ |
| `SET_DICT_SHARED(mp)` | `_PyObject_GC_SET_SHARED(mp)` | *empty* | 4 sites, all inside `#ifdef Py_GIL_DISABLED` ✓ |

Plus one non-`Py_GIL_DISABLED` pair: `ASSERT_CONSISTENT(op)` (`:666` / `:668`), switched
by `DEBUG_PYDICT`; the two arms differ only in the `check_content` argument (1 vs 0), and
both are inside `assert(...)`, so the entire consistency check — including its
`LOCK_KEYS_IF_SPLIT` — compiles out under `NDEBUG`. I confirmed no `ASSERT_CONSISTENT`
call site lies inside a `LOCK_KEYS` region (would self-deadlock on the non-reentrant
`_Py_LOCK_DONT_DETACH` mutex in a debug FT build): 20 call sites vs 5 `LOCK_KEYS`
regions, **0 overlaps**.

### `Objects/setobject.c` (2 pairs)

| macro | `Py_GIL_DISABLED` | default | note |
|---|---|---|---|
| `SET_IS_SHARED(so)` | `_PyObject_GC_IS_SHARED(so)` | `0` | 6 sites; 3 feed `free_entries(..., use_qsbr)`, 3 are inside `#ifdef Py_GIL_DISABLED` ✓ |
| `SET_MARK_SHARED(so)` | `_PyObject_GC_SET_SHARED(so)` | *empty* | 3 sites (`:90`, `:1561`, `:1564`); argument is a bare identifier in all 3, so the GIL arm discards nothing ✓ |

### `FT_ATOMIC_*` — what they degrade to, and whether anything breaks

`pycore_pyatomic_ft_wrappers.h` gives every `FT_ATOMIC_LOAD_x(v)` → `v` and every
`FT_ATOMIC_STORE_x(v, n)` → `v = n` under the GIL. **63 dual-armed names; the slice uses
17 of them at 133 sites.** The degradation is sound by construction: under the GIL every
one of these fields is reached only with the GIL held, which supplies both mutual
exclusion and the acquire/release ordering the atomic forms request. Two structural
observations:

- The FT arm takes `&value`, so the argument **must be an lvalue**; the GIL arm does not
  care. Misuse is therefore a *compile error on free-threaded builds and silently
  accepted on default builds* — the same one-directional-CI asymmetry as §2.2. There is
  no instance in the slice.
- `FT_MUTEX_LOCK`/`_UNLOCK`/`_LOCK_FLAGS` degrade to `do {} while (0)` — correctly
  `do`-wrapped, unlike the file-local lock macros in §2.4. Used at `dictobject.c:8273`
  and `:8281`, a balanced pair whose only intervening exit (`goto done` at `:8277`) lands
  *before* the unlock. Clean.
- The read-modify-write sites `dictobject.c:1936-1937`
  (`STORE_KEYS_USABLE(mp->ma_keys, mp->ma_keys->dk_usable - 1)`) do a **plain read and an
  atomic store**, and re-evaluate `mp->ma_keys` twice. This is correct here — the site is
  `insert_combined_dict`, combined keys have `dk_refcnt == 1` and are owned solely by the
  locked `mp`, so no concurrent writer exists. The **guarded twin** is
  `split_keys_entry_added` (`:242-250`), which handles the *shared*-keys case: it does the
  same non-atomic read but under `ASSERT_KEYS_LOCKED(keys)` and with an explicit
  ordering comment ("We increase before we decrease so we never get too small of a value
  when we're racing with reads") — that guard addresses concurrent *readers*, which is the
  threat model the combined-keys site does not have. Not a finding; recorded so the
  asymmetry is not re-discovered.

---

## 4. Classes bounded — clean, with denominators

**Allocation-size and index macros: clean. 40 use sites, 0 defects.**

| macro | definition | parameter parenthesized | body parenthesized | evaluations of each param |
|---|---|---|---|---|
| `USABLE_FRACTION(n)` | `(((n) << 1)/3)` | ✓ | ✓ | 1 |
| `GROWTH_RATE(d)` | `((d)->ma_used*3)` | ✓ | ✓ | 1 |
| `DK_SIZE(dk)` | `(((int64_t)1)<<DK_LOG_SIZE(dk))` | ✓ (via `DK_LOG_SIZE`) | ✓ | 1 |
| `DK_LOG_SIZE(dk)` | `_Py_RVALUE((dk)->dk_log2_size)` | ✓ | ✓ | 1 |
| `DK_MASK(dk)` | `(DK_SIZE(dk)-1)` | ✓ | ✓ | 1 |
| `DK_IS_UNICODE(dk)` | `((dk)->dk_kind != DICT_KEYS_GENERAL)` | ✓ | ✓ | 1 |
| `_PyDict_HasSplitTable(d)` | `((d)->ma_values != NULL)` | ✓ | ✓ | 1 |
| `PyDict_MINSIZE` / `PyDict_LOG_MINSIZE` / `SHARED_KEYS_MAX_SIZE` / `NEXT_LOG2_SHARED_KEYS_MAX_SIZE` / `PySet_MINSIZE` / `LINEAR_PROBES` / `PERTURB_SHIFT` | integer literals | n/a | n/a | n/a |

I checked each of the 8 `USABLE_FRACTION`, 1 `GROWTH_RATE`, 6 `DK_SIZE`, 5 `DK_MASK`,
11 `PERTURB_SHIFT` and 6 `LINEAR_PROBES` call sites for a side-effecting or
precedence-sensitive argument. **None has one.** The most complex arguments in the file —
`USABLE_FRACTION(DK_SIZE(okeys)/2)` (`:4232`), `USABLE_FRACTION((size_t)1<<log2_size)`
(`:850`), `USABLE_FRACTION((size_t)DK_SIZE(keys)) * es` (`:5174`) — all parse as intended
because both the parameter and the body are fully parenthesized.

Three structural facts that make this class smaller than the brief assumed:

- **`DK_ENTRIES`, `DK_UNICODE_ENTRIES` and `PySet_GET_SIZE` are `static inline` functions,
  not macros** (`pycore_dict.h:285/289`, `cpython/setobject.h:64`). They are structurally
  immune to multiple evaluation — 54 call sites need no review. `PySet_GET_SIZE` carries a
  same-named function-forwarding macro (`cpython/setobject.h:71`), and `ASSERT_DICT_LOCKED`
  (`dictobject.c:170`) uses the same trick; both rely on the C "blue paint" rule that
  blocks recursive expansion, and both are correct.
- **`DK_IXSIZE` does not exist.** `grep -rn DK_IXSIZE` over the whole tree at `4f3be1b5777`
  returns nothing — it was removed. Its denominator is **structurally zero**, not
  evidentially zero.
- **`pycore_setobject.h` defines exactly one macro: its own header guard.** The set-side
  macros the brief expected there live in `Objects/setobject.c` and
  `Include/cpython/setobject.h`. Denominator for "defect in a `pycore_setobject.h` macro"
  is **structurally zero**.

**Signed-arithmetic note, bounded.** `DK_SIZE` yields `int64_t`, so
`USABLE_FRACTION(DK_SIZE(k))` computes `((int64_t)1 << log2) << 1` in *signed* arithmetic
at `:711`, `:4232` and `:4251`, while `:850`, `:5174` and `:7262` cast to `size_t` first.
The inconsistency is real (someone thought the cast mattered), but signed overflow needs
`dk_log2_size >= 62`, i.e. a keys table of ≥2^62 slots whose index array alone is ≥2^65
bytes. `dictresize` already rejects `log2_newsize >= SIZEOF_SIZE_T*8` at `:2200` and
`PyMem_Malloc` at `:860` fails far earlier. **Unreachable; ACCEPTABLE, bound stated.**

**Header guards: clean, denominator 2.** `pycore_dict.h` → `Py_INTERNAL_DICT_H` (line
1-2, `#endif` at 459); `pycore_setobject.h` → `Py_INTERNAL_SETOBJECT_H` (1-2, `#endif` at
44). Both correct. `Include/cpython/setobject.h` correctly uses the `cpython/` house
pattern (`#ifndef Py_CPYTHON_SETOBJECT_H / #error "must not be included directly"`) rather
than a guard — that is the convention for that directory, not a defect.

**Macro scope / `#undef` discipline: clean, denominator 2.** Both `#undef`s in the slice
(`dictobject.c:798 #undef CHECK`, `:8292 #undef CASE`) correctly terminate a locally
scoped macro. `CASE` (`:8288`) is the `PY_FOREACH_DICT_EVENT` X-macro callback — properly
defined, used once, undefined. The macros that *stay* defined for the rest of the
translation unit (`CACHED_KEYS`, `dummy`, `frozendict_does_not_support`, the whole
`Py_GIL_DISABLED` block) are file-locals in a `.c` file with no trailing `#include`, so
they cannot leak into a header. Not a defect; §2.6 covers the naming half.

**Macro-introduced name capture: clean, denominator 3.** The only macros in scope that
introduce identifiers into the caller's scope are the critical-section family:
`_py_cs`, `_py_cs2`, `_cs_tstate` (`pycore_critical_section.h:255-291`). `grep` over both
`.c` files finds **zero** occurrences of any of the three outside the macro definitions —
no shadowing, no capture. (Note that a macro *parameter* named `key` — e.g.
`STORE_SHARED_KEY(key, value)` used at `:1977` as
`STORE_SHARED_KEY(ep->me_key, Py_NewRef(key))` — cannot capture the caller's `key`:
parameter substitution replaces names in the macro *body*, never inside the arguments.
Verified by expansion.)

**Critical-section pairing: clean, denominator 78 pairs.** Every
`Py_BEGIN_CRITICAL_SECTION*` is closed by a **same-arity** `END` (`scratchpad/cs_pairing.py`,
0 mismatches). This matters because `Py_BEGIN_CRITICAL_SECTION2` declares `_py_cs2` while
`Py_END_CRITICAL_SECTION()` releases `_py_cs`: a crossed pair would compile cleanly under
the GIL (both arms are bare braces) and release the wrong section under free-threading.
It does not occur.

**Control-flow escape from a lock region: clean, denominators 78 + 5 + 2.** No `return`,
`goto`, `break` or `continue` leaves any `Py_BEGIN/END_CRITICAL_SECTION*` region, any of
the 5 real `LOCK_KEYS`/`UNLOCK_KEYS` regions (`:1283-1285`, `:1317-1320`, `:1962-1981`,
`:2230-2261`, `:7317-7321`), or the 2 `LOCK_KEYS_IF_SPLIT` regions. Five `goto`s do occur
inside critical sections (`:4336/4343/4363/4371/4387` → `slow_exit:4390`; `:7392` →
`exit:7398`; `:7812/7821` → `exit_lock:7829`; `:8054` → `done:8062`; `:8081` →
`done:8094`) and **every one of their labels sits inside the region, above the `END`**.
The `#ifdef`-wrapped labels at `:7398`, `:8062` and `:8094` are correct: in the default
build the `goto`, the label and the enclosing braces all disappear together.

**Bare-`if` macro invocation: clean, denominator 91 macros × 875 sites.** Zero uses of any
in-scope macro as the unbraced body of an `if`/`else`/`for`/`while`. So §2.4 is entirely
latent.

**Assert-macro argument side effects: clean, denominator 45.** All 24
`_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` and 21 `ASSERT_DICT_LOCKED` call sites pass a
bare identifier or a plain cast of one. This matters because
`_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` evaluates its argument **twice** in the
free-threaded debug build (`Py_REFCNT(op)` and `_PyObject_CAST(op)`) and **zero** times in
the other three build configurations — a 2/0 evaluation split across build arms. No
exposure.

---

## 5. Previously-recorded findings touched by this pass — confirmed, not re-litigated

- **CPY-0096** (`dictobject.c:1971 insert_split_key`) — confirmed still present at
  `4f3be1b5777`: `_PyType_Modified_Unlocked(type)` is called at `:1971` between
  `LOCK_KEYS(keys)` at `:1962` and `UNLOCK_KEYS(keys)` at `:1981`, in direct contradiction
  of the comment at `:218-226` that forbids acquiring another lock inside `LOCK_KEYS`.
  From the macro-hygiene angle I add one datum: the `LOCK_KEYS`/`UNLOCK_KEYS` pair gives
  **no lexical scope at all** (unlike `Py_BEGIN/END_CRITICAL_SECTION`, which at least emits
  braces), so nothing marks the forbidden region for a reader or a tool.
- **CPY-0107** (`dictobject.c:1385 _Py_dict_lookup`) — confirmed: the
  `LOCK_KEYS_IF_SPLIT(dk, kind)` at `:1385` brackets `unicodekeys_lookup_generic`, which
  reaches `PyObject_RichCompareBool`. Same macro pair as §2.5; the pairing itself is
  correct (`kind` is latched in a local at `:1368`), the lock-order inversion is not.

---

## 6. Toolkit feedback

**Recall gap 1 — no scanner models a brace-emitting macro pair.** The §2.1 finding
(`dictobject.c:4380`) is invisible to every scanner in the slice baseline
(`scan_lock_discipline` reported **0 findings on `Objects/` tree-wide**, its denominator
is structurally zero for CPython because it looks for `PyThread_acquire_lock`-style
calls, not for `Py_BEGIN_CRITICAL_SECTION`). Concrete proposal: add a rule
`stranded_statement_after_jump` to `scan_lock_discipline.py`:

1. Pair `Py_BEGIN_CRITICAL_SECTION{,2,_MUTEX,2_MUTEX}` with a same-arity `END` (a
   *different-arity* pair is itself a finding — see §4 — and no scanner checks it today).
2. Inside each region, flag any `return` / `goto <label-outside-region>`.
3. Separately, flag any statement immediately following an unconditional
   `goto`/`return`/`break`/`continue` **where that jump is not the body of a brace-less
   control statement**. My naive version of this produced 89 candidates with 88 FPs, all
   one class; the brace-less-`if`-body filter reduces it to exactly 1 true positive across
   11,825 lines. That is a 1.1% → 100% precision jump from a single filter and is worth
   encoding.

**Recall gap 2 — no scanner compares the two arms of a dual-defined macro.** §2.2
(`LOAD_INDEX`'s trailing semicolon) and the whole §3 table came from reading. A cheap,
high-precision rule: for every name `#define`d twice within one `#ifdef X / #else /
#endif` group, compare (a) trailing-`;` presence, (b) whether the body is an expression
or a statement, (c) whether one arm is empty while the other has a non-assertion,
non-lock payload — the last is the CPY-0099 detector generalised, and it would have found
CPY-0099 mechanically. The slice gives a ready calibration set: 21 dual-arm pairs,
2 asymmetric (`LOAD_INDEX`, `STORE_INDEX`), 0 CPY-0099-shaped.

**Recall gap 3 — the `#if 0` blind spot.** §2.3 lives inside `#if 0`. A tree-sitter pass
over the raw source sees it; a preprocessed pass does not. The briefing's standing trap
"some markers only exist in comments / string literals" needs a third member: *some
defects only exist in disabled preprocessor arms*, and they are exactly the ones nobody
compiles and nobody reviews. Recommend the macro scanner run on **raw** source and report
`#if 0` residence as an attribute, not a suppression.

**Precision note on my own class.** The pure hygiene rules (missing parens, missing
do-while) had **very low yield here**: every size and index macro in the slice is
correctly parenthesized and single-evaluation (§4, denominator 40 sites). CPython's
`Objects/` macros are mature. The yield came entirely from the *build-arm asymmetry* and
*lock-macro control-flow* angles. If `macro-hygiene-reviewer` keeps a fixed checklist, the
first two items (parens, do-while) should be demoted to a denominator-reporting sweep and
the effort moved to arm comparison and lock-pair scoping.

**A prediction I tested and had to withdraw.** I expected `DECREF_KEYS(dk)`'s
unparenthesized `dk->dk_refcnt--` (GIL arm) to silently miscompile for a cast argument
while the FT arm caught it. I built the reduction (`scratchpad/armdiv.c`) and **both arms
error out** — the wrong parse `(struct DK*)x->dk_refcnt--` is rejected in the realistic
argument shapes. Reporting it as a hazard would have been an unverified assertion.
Recording the negative so the next pass does not re-run the experiment.

---

## 7. Noticed outside slice

- `Include/internal/pycore_critical_section.h:64` — `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED`
  is a bare `if` with no `do { } while (0)`, and is empty in 3 of its 4 build arms; it is
  used tree-wide, not only here.
- `Include/internal/pycore_pyatomic_ft_wrappers.h:24` and `:30` define
  `FT_ATOMIC_STORE_PTR` **twice, identically**, six lines apart (both inside the
  `Py_GIL_DISABLED` arm) — a benign duplicate `#define`, but it means the file has 126
  directives for 63 names + 1 stray.
- `Objects/dictobject.c:7602-7630` `_PyObject_ManagedDictValidityCheck` is entirely inside
  `#if 0` yet is a non-static function referenced by `pycore_object.h` — worth a separate
  dead-code pass (`obj-object` slice).

---

## Artifacts

Scripts used (in this session's scratchpad, `/tmp/claude-1000/.../scratchpad/`):
`macro_audit.py` (definition + use-site denominators, brace-less-`if` scan),
`cs_escape.py` (control-flow escape from lock regions), `cs_pairing.py` (BEGIN/END arity
pairing), `dead_after_jump.py` (§2.1 sibling hunt), and three minimal C reductions —
`loadidx.c` (§2.2, compiled both arms), `dangle.c` (§2.4, compiled both arms),
`armdiv.c` (the withdrawn prediction in §6).
