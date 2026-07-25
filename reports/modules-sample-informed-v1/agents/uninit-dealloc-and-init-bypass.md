# uninit-dealloc + init-bypass — Modules/ sample (informed)

Toolkit v0.8.0. Target `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0).
Builds used: `/home/danzin/projects/python_build_matrix/builds/{debug,release}-{gil,ft}-nojit[-asan]`
@ `a1d580430c8`. **Every file cited below is byte-identical between `a1d580430c8` and
`4f3be1b5777`** (`git diff --quiet` on `_elementtree.c`, `_pickle.c`, `_csv.c`,
`_datetimemodule.c`, `_zoneinfo.c`, `_sre/sre.c`, `hmacmodule.c`, `_asynciomodule.c`).

## Scanner volume

| scanner | raw (sample) | confirmed | dismissed | precision |
|---|---|---|---|---|
| `scan_uninit_dealloc` | 1 | 1 | 0 | **100%** |
| `scan_init_bypass` | 0 | — | — | n/a — **denominator 2**, see below |

Hand-audit added 1 FIX outside the sample (`_asynciomodule.c`), found by chasing the
init-bypass recall gap rather than by either scanner.

---

## Findings

### FIX

#### 1. `Modules/_asynciomodule.c:2788` · `_asyncio_Task_get_context_impl` — **new, reproduced SIGSEGV**

```c
static PyObject *
_asyncio_Task_get_context_impl(TaskObj *self)
{
    return Py_NewRef(self->task_context);
}
```

`Task_slots` (`:2936`) wires `{Py_tp_init, _asyncio_Task___init__}` with
`{Py_tp_new, PyType_GenericNew}`. `PyType_GenericNew` allocates a **zeroed** instance and
never runs `tp_init`, so `Task.__new__(Task)` yields `task_context == NULL`;
`task_context` is written only in `_asyncio_Task___init__` (`:2324` / `:2329`).
`Py_NewRef(NULL)` dereferences `ob_refcnt`.

**Guarded twin — literally the two neighbouring functions.** `_asyncio_Task_get_coro_impl`
(`:2774`) and `_asyncio_Task_get_name_impl` (`:2808`) both open with
`if (self->task_<x>) { ... } Py_RETURN_NONE;`. Wider family: 16 sites use
`ENSURE_FUTURE_ALIVE(state, self)` / `future_ensure_alive()` (`:401`, `:412`), which raises
`RuntimeError("Future object is not initialized.")` — that is why every *other*
`Task`/`Future` method in the sweep below returned an exception instead of crashing.
`get_context` is the single hole.

**Reachability:** one line of pure Python, no arguments, no threads, no OOM.
`asyncio.Task` *is* `_asyncio.Task` when the C accelerator is importable. The realistic
form is a subclass whose `__init__` forgets `super().__init__()` — the exact scenario
`_pickle`'s `issue3664` guard and gh-144330 were written for.

Reproduction (all four builds, plus ASan; core dumped in every case):

```
$ gdb -batch -ex run -ex 'bt 3' --args .../debug-gil-nojit/python \
      -c 'import _asyncio; _asyncio.Task.__new__(_asyncio.Task).get_context()'
Program received signal SIGSEGV, Segmentation fault.
_asyncio_Task_get_context_impl (self=0x7ffff707f060) at ./Modules/_asynciomodule.c:2788
2788        return Py_NewRef(self->task_context);
#0  _asyncio_Task_get_context_impl (self=0x7ffff707f060) at ./Modules/_asynciomodule.c:2788
#1  _asyncio_Task_get_context (self=..., _unused_ignored=0x0) at ./Modules/clinic/_asynciomodule.c.h:1492
#2  method_vectorcall_NOARGS (...) at Objects/descrobject.c:448

debug-gil-nojit   rc=139     release-gil-nojit rc=139
debug-ft-nojit    rc=139     release-ft-nojit  rc=139
debug-gil-nojit-asan: AddressSanitizer: SEGV on unknown address (pc 0x7b3d51ea648c)

# realistic path — subclass that forgets super().__init__()
class MyTask(asyncio.Task):
    def __init__(self, *a, **k): pass
MyTask.__new__(MyTask).get_context()          # rc=139
```

**Class:** FIX. Shape = `init-bypass-null-deref` (gh-152954 / gh-144330 family;
same class as the run's `bytearray.__new__(bytearray).append(1)` and
`super.__new__(super).__get__(1)`).
**Fix:** `if (self->task_context == NULL) Py_RETURN_NONE;` — matching `get_coro` /
`get_name` two functions away. (Raising via `ENSURE_FUTURE_ALIVE` would also work but
changes the documented return type for a legitimately-uninitialised task.)
**Merits a new catalog entry (CPY-00xx).**

I swept every attribute and no-arg method of `_asyncio.Future`, `_asyncio.Task`,
`_pickle.Pickler`, `_pickle.Unpickler`, `_random.Random` and `_struct.Struct` after
`T.__new__(T)`: `Task.get_context` is the **only** crash. No sibling to propagate to
inside that family.

### CONFIRM-ONLY (catalogued, not re-litigated)

#### 2. `Modules/_elementtree.c:2367` · `create_elementiter` — CPY-0016, still live

Confirmed in one line: `it->parent_stack_used` is written at `:2380`, *after* the
`Py_DECREF(it)` at `:2377` on the `PyMem_New` failure path, and `elementiter_dealloc:2178`
reads it as the loop bound over a NULL `parent_stack`. Still reproduces:

```
$ run_oom_sweep.py --python .../debug-gil-nojit/python \
    --setup 'import _elementtree; e = _elementtree.Element("a")' \
    --code  'it = e.iter()' --max-n 12
"outcome_counts": {"memory_error": 2, "segv": 1, "completed": 9}
"first_crash": {"n": 2, "outcome": "segv", "returncode": -11}
  ... _elementtree...so +0x5d4d  <-  _Py_Dealloc+0x12e  <-  ...
"verdict": "REPRODUCED — allocation failure crashes the interpreter"
```

Deliberately **no warm-up `iter()` in `--setup`**: a prior iterator would have been freed
through `elementiter_dealloc`, which sets `parent_stack_used = 0` before `tp_free`, so a
recycled same-type block comes back clean and masks the crash (the gh-151815 lesson).
The setup ran unarmed; crash window is one allocation wide (`K=2` only).

---

## New siblings of known shapes

**None found for `dealloc-of-uninitialized-object` — and that is a measured result, not a
skipped search.** I enumerated *every* non-zeroing allocation in all of `Modules/` (not
just the 12-file sample) that is followed by a free of the same variable inside the
constructor: 36 sites. Hand-read 31 of them (5 are `Modules/_testcapi*` deliberate-crash
helpers). Every one either NULL-inits all destructor-visible members before the first
fallible call, or the failing call *is* the assignment (`dp->di_dbm = dbm_open(...)`,
`wo->encoding = _PyMem_Strdup(...)`, `self->write = ...`).

Two shapes deserve naming as **guarded twins** for the CPY-0016 loop-bound bug, because
they are the exact fix pattern for "count set before the array is filled":

- `Modules/_zoneinfo.c:1268` `load_data`'s `error:` label decrefs with a **separate
  progress counter** (`ttinfos_allocated`), not `self->num_ttinfos`, then NULLs
  `self->_ttinfos` so `zoneinfo_dealloc:404`'s `num_ttinfos` loop is skipped.
  `build_ttinfo` (`:898`) NULLs all three out-members first; `build_tzrule` (`:1994`)
  builds into a local `_tzrule rv = {{0}}` and commits `*out = rv` only on success.
- `Modules/_sre/sre.c:1895` `_sre_template_impl` calls `Py_SET_SIZE(self, i)` *before*
  `Py_DECREF(self)`, shrinking the object so `template_clear`'s `Py_SIZE(self)` loop only
  touches fully-written slots.

`Modules/pyexpat.c:2624` `clear_handlers` opening with `if (self->handlers == NULL) return;`
is the third twin, covering the `PyMem_New(handlers)`-failed early free at `:1149`.

---

## Classes bounded (clean negatives, with evidence)

- **12-file sample, uninit-dealloc:** 15 non-zeroing allocation sites; 14 ACCEPTABLE,
  1 = CPY-0016. `_pickle.c` (`457`, `1144`, `1713`, `5245`, `7745`) initialises every
  member in one uninterrupted block before any fallible step — `Pdata_New`'s only fallible
  step assigns the member it would leak (`self->data`) and `Py_SET_SIZE(self,0)` precedes
  it. `_csv.c` (`1091`, `1560`) is the textbook guarded form. `itertoolsmodule.c`
  (`628`, `756`, `1027`, `1069`) and `_collectionsmodule.c` (`1926`, `2105`) have **no
  fallible step at all** between allocation and the last member write.
- **`_zoneinfo.c` / `_struct.c` / `_json.c` / `_datetimemodule.c` / `_queuemodule.c` /
  `_heapqmodule.c` / `_randommodule.c`:** zero non-zeroing allocator sites. All construct
  through `tp_alloc` / `PyType_GenericAlloc`. The TZif mid-parse failure the briefing
  predicted is real but fully handled (see the `load_data` twin above).
- **`_struct.c:2328`** `PyType_GenericAlloc` + `PyObject_GetBuffer` failure → `Py_DECREF`:
  the zeroed `Py_buffer` makes `PyBuffer_Release` a no-op. ACCEPTABLE.
- **init-bypass reachability in the 12 sample files:** empirically clean. I ran
  `T.__new__(T)` + every attribute/no-arg method for `Pickler`, `Unpickler`, `Random`,
  `Struct`, `deque`, `defaultdict`, `Element`, `TreeBuilder`, `XMLParser`, `SimpleQueue`,
  `ZoneInfo`, `_json.make_scanner`/`make_encoder` — 42 probes, zero crashes. The guards
  are explicit: `_pickle.c:4881` (`"Pickler.__init__() was not called by %s.__init__()"`,
  issue3664) and `:7286` for `Unpickler`; `_struct.c:2112` `ENSURE_STRUCT_IS_READY`;
  `_collectionsmodule.c:2309`/`:2383` treat a NULL `default_factory` as `None`;
  `_json`/`_zoneinfo` have real argument-taking `tp_new`s so `T.__new__(T)` raises
  `TypeError` before any field is read.

---

## Toolkit assessment

### `scan_uninit_dealloc` — precision 1/1, recall verified by hand over all of `Modules/`

The v0.8 rewrite holds up. The `destructor_read` tier is what recovered `_elementtree`
(`parent_stack_used` is a `Py_ssize_t`, invisible to the `pointer` tier), and the
`low`-confidence label was correctly calibrated — it *is* a real crash. My exhaustive
36-site hand-audit of `Modules/` produced **no missed instance** of the direct shape, so
the "1 finding over 380 files" is earned, not accidental.

**One real recall hole found by reading: a project-local `tp_alloc` that does not zero.**
`Modules/_datetimemodule.c:879 time_alloc` and `:891 datetime_alloc` are `PyObject_Malloc`
+ `_PyObject_Init`, with the file's own comment saying so:

> `/* ... If there's not enough memory, they return NULL after setting MemoryError.`
>  `All data members remain uninitialized trash. */`  (`:861-862`)

They are installed positionally (`:5382 time_alloc, /* tp_alloc */`, `:7349 datetime_alloc`).
The scanner's FP taxonomy asserts "`tp_alloc(type, n)` … zero the object, so a following
early free is safe" — false for these two types. `time_dealloc`/`datetime_dealloc`
`switch` on the scalar `hastzinfo` to decide whether to `Py_XDECREF(self->tzinfo)`: the
**exact blake2 `impl` shape**. Today all nine `tp_alloc` call sites in that file set
`hastzinfo` in the statement immediately after the allocation, so there is no live bug —
but the file is one refactor away from one, and the scanner cannot see it. Tree-wide there
are exactly two non-zeroing `tp_alloc`s (these); `bytes_alloc`, `_PyType_AllocNoTrack` and
`PyType_GenericAlloc` all zero.

### `scan_init_bypass` — **recall gap, not a true negative** (with numbers)

The sample's 0 is **silence**. Per-file denominators (`total_nullable_fields`), run
individually on each of the 12 files:

| file | nullable fields | by reason |
|---|---|---|
| `_collectionsmodule.c` | 2 | `deletable_member` 2 |
| the other **11 files** | **0** | — |

So the sample denominator is **2**, all `deletable_member`, and **`new_bypass` = 0** —
despite the sample containing **four** genuinely bypassable types:

| slot table | `Py_tp_init` | `Py_tp_new` |
|---|---|---|
| `_pickle.c:5367 pickler_type_slots` | `_pickle_Pickler___init__` | `PyType_GenericNew` |
| `_pickle.c:7876 unpickler_type_slots` | `_pickle_Unpickler___init__` | `PyType_GenericNew` |
| `_randommodule.c:581 Random_Type_slots` | `random_init` | `PyType_GenericNew` |
| `_collectionsmodule.c:2507 defdict_slots` | `defdict_init` | *(absent — inherited)* |

**Root cause — `scan_init_bypass.py:451-457`, the whole-file kill switch.** For the
`PyType_Spec` form the scanner disables `new_bypass` for an entire file if
`_TP_NEW_TOKENS_RE` (`\bPy_tp_new\b|\.tp_new\s*=|\bDISALLOW_INSTANTIATION\b`) matches
**anywhere** in it. That regex is **token-based, not value-based** — it fires on
`{Py_tp_new, PyType_GenericNew}`, even though `PyType_GenericNew` is in the scanner's own
`_INHERITED_NEW = {"0", "NULL", "PyType_GenericNew"}`. And `DISALLOW_INSTANTIATION` in the
pattern matches `Py_TPFLAGS_DISALLOW_INSTANTIATION` on *any* type in the file, killing all
its siblings. The v0.8 work fixed exactly this for the *positional* form
(`_positional_bypassable_inits` does per-block pairing and value comparison) and left the
spec form — the dominant form in `Modules/` — on the old coarse switch.

**Measured blast radius.** Pairing `Py_tp_init` with the `Py_tp_new` of the *same* slot
table across `Modules/` + `Objects/` + `Python/`: 58 spec tables carry a `Py_tp_init`;
**21 of them (36%) are bypassable and are silenced by the kill switch** — including
`_asynciomodule.c` Future *and* Task (both `PyType_GenericNew`), all seven `_ctypes`
metatypes, `_decimal` `signaldict`, `Python-ast.c` `AST_type_slots`, `selectmodule.c`
`kqueue_event`, plus the four sample types above.

**Proof the gap is causal, not theoretical.** Deleting the two
`{Py_tp_new, PyType_GenericNew},` lines from a *copy* of `_asynciomodule.c` (removing the
kill-switch token without changing any other semantics) and re-running the **unmodified**
scanner:

```
--- Modules/_asynciomodule.c              findings 0   nullable 0   {}
--- ks/_asynciomodule.c (token removed)   findings 1   nullable 7   {'new_bypass': 7}
     2788 _asyncio_Task_get_context_impl  task_context  medium  new_bypass
```

Exactly the reproduced SIGSEGV, zero false positives.

**Cost of the fix, measured.** I implemented the proposed per-slot-table pairing in a copy
of the scanner (mirroring `_positional_bypassable_inits`: pair `Py_tp_init` with the
`Py_tp_new` of the same `PyType_Slot[]`, treat absent/`0`/`NULL`/`PyType_GenericNew` as
bypassable, and consult the referencing `PyType_Spec` for
`Py_TPFLAGS_DISALLOW_INSTANTIATION`):

| corpus | current | fixed |
|---|---|---|
| `Modules/` | 24 findings / 103 nullable / 10 files | **26 findings / 123 nullable / 13 files** |
| `Objects/` | 3 findings / 38 nullable | **3 / 38 — bit-identical** |
| `Python/` | 0 / 0 | **0 / 0 — identical** |

The two new `Modules/` findings are `_asynciomodule.c:2788` (the reproduced SIGSEGV) and
`_pickle.c:1103 _Pickler_Write` reading `output_buffer` — the latter is a **false
positive of the documented interprocedural limitation**: every caller passes through
`_pickle_Pickler_dump_impl:4881`, whose `issue3664` guard rejects an uninitialised
Pickler. Net: **+1 reproduced SIGSEGV for +1 known-class FP, and zero regression on
`Objects/`/`Python/`.**

### Prompt problems

- The FP taxonomy's "Zeroing allocator … `tp_alloc(type, n)` … zero the object" reads as
  unconditional and is wrong for a type that installs its own `tp_alloc`. It should say
  *resolve the type's actual `tp_alloc`*, and name `_datetimemodule.c`'s pair as the
  standing counterexample — otherwise an agent (or the scanner) dismisses that whole file
  by reflex.
- The `scan_init_bypass` docstring already carries the right `total_nullable_fields`
  canary and it worked. But the canary is **whole-corpus** in the envelope; the sample
  JSON kept the `Modules/`-wide `total_nullable_fields: 103` next to `findings: []`, which
  reads as "big denominator, clean" when the sample denominator was actually 2. The
  `_sample_filter` post-processing must recompute the denominator, not just the findings.

### Ranked tuning proposals

1. **(highest value) Replace the whole-file kill switch in `scan_init_bypass.py` with
   per-slot-table pairing for the `PyType_Spec` form** — a `_spec_bypassable_inits()`
   mirroring the existing `_positional_bypassable_inits()`. Measured above: +1 reproduced
   SIGSEGV, +1 known-class FP, 0 regression, and it un-silences 21 of 58 spec tables
   tree-wide (36% of that form's recall). This is the same defect class v0.8 already fixed
   on the positional side; the spec side was simply not carried over.
   *Regression test:* `_asynciomodule.c:2788` must be reported;
   `Objects/` must stay at exactly 3 findings / 38 nullable fields.
2. **Make `scan_uninit_dealloc` resolve `tp_alloc` instead of trusting the name.** Keep a
   hardcoded non-zeroing list (`time_alloc`, `datetime_alloc` — the only two in-tree) and,
   more generally, flag any file-local `allocfunc` whose body is `PyObject_Malloc` /
   `PyMem_Malloc` + `_PyObject_Init` without a `memset`, then treat `type->tp_alloc(...)`
   in that file as non-zeroing. Cheap (one regex over `allocfunc`-typed definitions), and
   it closes the only recall hole a full hand-audit of `Modules/` surfaced.
3. **Recompute denominators when a run is sample-filtered.** `<run>/scanners/*.sample.json`
   currently keeps the corpus-wide `summary`, `total_nullable_fields`,
   `files_with_nullable_fields` and `nullable_fields_by_reason` beside a filtered
   `findings` list. For `scan_init_bypass` that inverted the canary's meaning (`103` shown,
   `2` actual) and would have justified a false "clean negative". Every denominator field
   in the envelope should be recomputed over the retained files, or explicitly renamed
   `*_corpus`.

*(Runner-up, lower confidence: `scan_uninit_dealloc`'s "destructor must be in the same
file" rule was not exercised by this sample — every constructor I audited had its
`tp_dealloc` beside it. `Modules/_io/`, `_sqlite/` and `_ctypes/` split types across
translation units and would exercise it; worth a targeted run before claiming the rule is
harmless.)*
