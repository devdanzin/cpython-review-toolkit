# Changelog

All notable changes to this project will be documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Fixed — six defects measured by the informed `Modules/` review

Every number below was measured on CPython main @ `4f3be1b5777` (3.16.0a0) over
all of `Objects/` + `Modules/` + `Python/`, before and after.

- **`scan_refcounts.py`: `init_not_reinit_safe` was inert, and the v0.8 notes
  wrongly certified it as clean.** v0.8 rewrote `_is_tp_init` to require real
  slot registration, saw the rule go to zero, and recorded *"empty on CPython —
  demoted to a footnote"*. The rule was **silent, not clean**: Argument Clinic
  emits `<Type>___init___impl` while the registered slot is the generated
  `<Type>___init__` in `clinic/*.c.h`, often with a further hand-written wrapper
  in between (`Modules/_struct.c` registers `s_init` → `Struct___init__` →
  `Struct___init___impl`), so requiring registration of the *impl* resolved
  **0 of the 80** `__init__` bodies in the tree. Detection now accepts the
  clinic name as proof of tp_init-hood, and the rule expresses the real hazard:
  a re-callable `__init__` that **destroys and replaces state an outstanding
  iterator/view still reads through a stored owner pointer** (≤2 hops into
  file-local helpers, since the mutation is usually two calls away). Note the
  polarity flip — `Py_XSETREF`/`Py_CLEAR`/free-then-assign used to *suppress*
  the rule as evidence of re-init safety; they are the proof of the hazard.
  **0 → 1 finding tree-wide, 1 true positive, 0 false positives**, denominator
  80 bodies / 9 with a destroy-and-replace. The true positive is a **reproduced
  heap disclosure** in `Modules/_struct.c`: `s = struct.Struct("i");
  it = s.iter_unpack(b"\0"*8); next(it); s.__init__("100i"); next(it)` returns a
  100-tuple of which 73 words were live heap on a release build, and trips
  `assert(self->index + self->so->s_size <= self->buf.len)` (`_struct.c:2274`)
  on a debug build.
- **`scan_error_paths.py`: `PyErr_Occurred()` was counted as a narrowing.** It
  is the *failure test*, not a narrowing, so listing it in
  `_PYERR_CLEAR_GUARD_RE` suppressed every
  `if (x == -1 && PyErr_Occurred()) PyErr_Clear();` — the most common written
  form of the bug. `unconditional_pyerr_clear`: `Objects/` 23 → 27, `Modules/`
  39 → 44, `Python/` 68 → 70. Hand-checked: **11 net-new candidates, 5 true
  positives** (all four `Modules/itertoolsmodule.c` `islice_new` clears, plus
  `Objects/bytes_methods.c:608` `_Py_bytes_contains`). The dominant new FP class
  — a `PyLong_As*` on an operand the function already `PyLong_Check`-ed, where
  only `OverflowError` can be pending — is suppressed, removing 6 of 9 hand-found
  FPs without losing a true positive.
- **`scan_error_paths.py`: new rule `pylong_sentinel_no_errcheck`.** `PyLong_As*`
  compared `== -1` with no `PyErr_Occurred()` narrowing: `-1` is both the error
  sentinel and the honest conversion of the integer `-1`, so an ordinary input
  takes the failure branch with **no exception set**. **4 candidates tree-wide,
  4 true positives, 0 false positives** — `Modules/_zoneinfo.c:1073, :2314,
  :2324, :2334`, with the guarded twin ten lines above one of them at `:2304`.
  Two of the four abort a debug build through five public `zoneinfo` entry
  points.
- **`scan_pyerr_clear.py`: the re-raise gate was inverted for rule 3, and a bare
  errstate probe dropped the site.** Suppressing on *any* following `PyErr_Set*`
  treated "substitute a fixed `ValueError` for whatever the user's `__index__`
  raised" as a mitigation, when that substitution **is** the bug; rule 3 now
  requires an information-preserving re-raise (restore / chain / errno-derived),
  while rule 2 keeps the broad test (a function like `_PyErr_SetKeyError` clears
  first *because* the API building the replacement must not run with an
  exception set). Separately, an innermost `if (PyErr_Occurred())` names no
  failing call — it is a nested re-test of the same failure — so rule 3 now
  walks one condition outward through that shape only. `islice_new` **1/4 →
  4/4**; `Modules/` `pyerr_clear_unfiltered_after_python_call` 16 → 20;
  `Objects/` and `Python/` **bit-identical**, and `on_success_path` unchanged
  everywhere (the naive full-disable probe added 3 known-FP clears there).
- **`scan_init_bypass.py`: the `PyType_Spec` kill switch silenced 36% of slot
  tables.** The whole-file switch fired on the *token* `Py_tp_new` even though
  `{Py_tp_new, PyType_GenericNew}` is the canonical **bypassable** wiring and is
  in the scanner's own `_INHERITED_NEW`; `Py_TPFLAGS_DISALLOW_INSTANTIATION` on
  one type also killed every sibling in the file. **21 of the 58 slot tables
  carrying a `Py_tp_init` tree-wide** were silenced. Replaced with per-slot-table
  pairing (`_spec_bypassable_inits`, mirroring the positional form, and
  inspecting the whole enclosing initializer because a `PyType_Slot[]` may list
  `Py_tp_new` before `Py_tp_init`), consulting the referencing `PyType_Spec` for
  the disallow flag. `Modules/` **24 → 26** findings / 103 → 123 nullable fields
  / 10 → 13 files; `Objects/` and `Python/` **bit-identical**. The two new
  findings are `Modules/_asynciomodule.c:2788` — a reproduced SIGSEGV
  (`_asyncio.Task.__new__(_asyncio.Task).get_context()`, exit 139 on all four
  build variants, guarded twins `get_coro:2774` and `get_name:2808` immediately
  around it) — and one known-class interprocedural FP at `_pickle.c:1103`.
- **`scan_recursion_guards.py`: the `PyObject_Hash` alias was invisible, and the
  `*_getstate` idiom was mis-rated.** `_PyObject_HashDictKey`
  (`pycore_object.h:840`) is a `Py_ALWAYS_INLINE` wrapper whose tail is
  `return PyObject_Hash(op);` — **27 call sites tree-wide** were unreachable,
  8+ in `Objects/dictobject.c` plus `Modules/_collectionsmodule.c:2592`
  (`collections.Counter`, a confirmed ASan stack overflow). Added to the
  vocabulary; `Objects/` `missing_recursion_guard` 27 → 43, every new site
  correctly self-rated `low` `hash_entry_point`. A `bound_zero_excluded` set
  (`PyObject_GenericHash` / `Py_HashPointer` / `Py_HashBuffer`) now records the
  deliberate exclusions in the envelope. Separately, `_TEMP_CTOR_RE` now
  survives one hop into a file-local `return <ctor>(...)` helper, and a
  `Py_BuildValue` whose format holds no object codes is bound-**0** — which
  retires the `delta_hash` false positive (`delta_getstate` is
  `Py_BuildValue("iii", ...)`) without touching the correctly-bounded
  `time_hash` / `datetime_hash` / `deque_richcompare` dismissals or the one true
  positive, `Modules/_sqlite/row.c` (SIGSEGV at depth 400 000, sites `:235` and
  the load-bearing `:239`).
- **`scan_uninit_dealloc.py`: `tp_alloc` was assumed to zero.** It only does when
  it *resolves* to a zeroing allocator. `Modules/_datetimemodule.c` installs
  `time_alloc` (`:879`) and `datetime_alloc` (`:891`) — `PyObject_Malloc` +
  `_PyObject_Init`, no `memset` — and the file's own comment says "All data
  members remain uninitialized trash". The scanner now resolves the slot
  (`_nonzeroing_tp_allocs`) and treats `->tp_alloc(...)` as non-zeroing in a file
  that installs one. Those two are the only in-tree instances; there is no live
  bug behind them today and finding counts are **unchanged** on `Objects/`,
  `Modules/` and `Python/`.

### Added — two recall gaps closed in `scan_refcounts.py`

The `borrowed_ref_across_call` hazard model counted **releases only**
(`if not releases: continue`), so a borrowed value that *escapes via `return`*
or is *dereferenced/called* was never a hazard. Three ASan-confirmed
heap-use-after-frees sat behind that one line. Measured over `Objects/` +
`Modules/` + `Python/` at CPython main `4f3be1b5777`; existing rules unchanged
(`borrowed_ref_across_call` still fires exactly on `itertoolsmodule.c:3988` and
`:4018`).

- **`slot_transfer_across_call`** — `local = obj->fld` … Python-reaching call …
  `obj->fld = <new>` … `return local`. The "we'll either return it or keep it in
  the slot" transfer idiom performed across a re-entrancy window, so a
  re-entrant call performing the same transfer leaves two owners for one
  reference. **1 finding tree-wide, 1 true positive**:
  `Modules/itertoolsmodule.c:3633` `count_nextlong` (reproduced: ASan
  heap-use-after-free, the freed counter recycled as a `dict`). The ordering
  gate — the slot overwrite must come *after* the call — suppresses
  `Modules/_tkinter.c` `TimerHandler`.
- **`stale_slot_use`** — `local = obj->fld` … Python-reaching call …
  `Py_CLEAR(obj->fld)` reachable … `local` **dereferenced or called**. Worse
  than a double-DECREF: `slot_tp_iternext` reads `Py_TYPE(self)` out of the
  freed block. **2 findings tree-wide, both reproduced ASan
  heap-use-after-frees**: `Modules/itertoolsmodule.c:210` `batched_next` and
  `:1711` `islice_next`. (`Objects/iterobject.c:80` also matches but is
  suppressed as a duplicate of the narrower `stale_slot_decref`, which names the
  exact fix.) Three gates carry the precision: a clear that *precedes* the first
  Python-reaching call is a completed ownership transfer
  (`Modules/_elementtree.c` `elementiter_next`); a local re-read from the slot
  after the call is the guarded twin (`pairwise_next:364`); and
  `_reassigned_before`.
- Both rules required two new primitives. **Runtime type-slot dispatch is
  Python-reaching**: `iternext = *Py_TYPE(x)->tp_iternext; … iternext(x)` and
  the inline `(*Py_TYPE(x)->tp_iternext)(x)` form are invisible to a name table,
  and without them `batched_next`/`islice_next` stay invisible even after the
  hazard set is widened. A *statically named* type (`PyUnicode_Type.tp_hash`) is
  deliberately not matched. **Loop-carried exposure**: in `batched_next` the
  borrowed local appears once textually, and the danger is that iteration N+1's
  use follows iteration N's call, so the search window widens to the end of the
  enclosing loop.
- **New false-positive class, encoded and documented: type-constrained
  operand.** `Objects/enumobject.c:196` `increment_longindex_lock_held` is a
  structural clone of `count_nextlong` — comment text and all — and is *safe*,
  because `en->one` is `_PyLong_GetOne()` and `en_longindex` is only ever a
  `PyLong`, so `PyNumber_Add` resolves to `long_add` and no user code runs. The
  discriminator: a parameter counts as type-pinned only when the function
  coerces it through an int-producing conversion **of itself**
  (`start = PyNumber_Index(start)`), not when it merely receives a default
  (`long_step = _PyLong_GetOne()`) — which is exactly why `count`'s untyped
  slow-path step is *not* pinned.
- `data/cpython_non_bugs.md` gains that class plus a carve-out from "borrowed
  ref under a known-live owner": **a raw `PyMem_Malloc` buffer hanging off a
  live object is not protected by its owner** (`_struct.c` `s_codes`,
  `_zoneinfo.c`'s `StrongCacheNode` chain, `_elementtree.c`'s `extra` — three
  reproduced crashes the taxonomy previously argued for dismissing).

### Fixed — `scan_ft_races.py` T1 dropped real findings and is retargeted

- **T1 emitted one finding per *field name* per file, not per site**
  (`reported: set[str]` in `_check_t1`), and paired accesses by bare member name
  with no struct resolution. Measured cost, twice in one run: `itertoolsmodule.c`
  reported `isliceobject.cnt` and **discarded `count_repr:3680`** — TSAN-0006,
  the run's own calibration entry — and `_collectionsmodule.c` collapsed 13
  `counter` accesses to the single provably-safe constructor write. *(The shared
  `deduplicate_findings` was not the cause: its key is already
  `(type, file, line)`.)* T1 now emits **one finding per unsynchronised site**
  and pairs by the receiver's declared struct type, so `member` reads
  `Type.field`.
- **Retargeted at "guarded writer / unguarded reader of the same field"**, which
  is the actual shape of every catalogued instance — gh-153298 (`ga_parameters`
  / CPY-0025), gh-128714 (`func.__annotations__` / CPY-0029), gh-153908
  (`count_repr`). New type `guarded_writer_unguarded_reader`; a plain **pointer**
  read is ranked above a scalar one (`medium` vs `low`) because a stale pointer
  handed to `PyObject_Repr` is a use-after-free an atomic load cannot fix.
  `atomic_plain_asymmetry` is kept for the atomic-twin case and gains the
  **mixed-discipline** polarity: an atomic *reader* outside any lock racing a
  plain *writer* under a critical section (`_collectionsmodule.c`
  `dequeiter_len:2049` vs `dequeiter_next_lock_held:1986`, reproduced under
  TSan).
- **The pre-publication suppression is wired into T1** and widened: the
  `_INITIALIZER_NAME_RE` name test, a receiver assigned from `tp_alloc` /
  `PyType_GenericAlloc` (the `_ALLOCATOR_CALL_RE` did not match
  `type->tp_alloc(...)`, so every constructor's stores looked shared),
  module-exec / `PyInit_*`, `initialize`-spelled constructors, the
  destructor/teardown family, `PyType_Ready` slot inheritance, fork-child and
  assert-only checkers, and sentinel stores with no prior read. A guarded write
  into a freshly allocated object no longer counts as a guarded *twin* either
  (`deque_copy_impl`, `deque_iter`).
- **Three new lock-recognition paths**, each a measured false-positive class:
  Argument Clinic `@critical_section` (the lock is emitted into
  `clinic/<file>.c.h`, so the `_impl` looked entirely unsynchronised — the
  single largest FP class), SCREAMING_CASE lock macros (`LOCK_WEAKREFS`), and a
  **transitive** one-hop-and-beyond caller check: `count_nextlong` takes no lock
  and is not named `*_lock_held`, but its only free-threaded caller wraps it in
  `Py_BEGIN_CRITICAL_SECTION(lz)`. Chains matter — `_io/textio.c` reaches
  `_textiowrapper_writeflush` only through clinic-guarded impls.
- **An asymmetry cap** (≤4 unsynchronised sites across ≤2 functions per field)
  keeps the rule on the incomplete-fix shape it exists to find. Without it the
  retarget emits ~1,100 findings tree-wide; a field with a dozen unguarded
  accessors is an un-hardened module (the `_pickle.c` case) and belongs in one
  POLICY finding, not N FIXes.
- Net effect on `Objects/` + `Modules/` + `Python/`: **180 → 206 findings**
  (T1 113 → 141, per-site instead of per-field), line accuracy **92.8% → 98.1%**,
  runtime 27s → 12s. All eight must-catch sites are present, including the two
  the collapse had been discarding (`itertoolsmodule.c:3678`/`:3680`,
  `_collectionsmodule.c:1986`).

### Changed
- `data/cpython_non_bugs.md`: the "Zeroing allocator" entry amended — `tp_alloc`
  removed from the unconditionally-zeroing list, with the `_datetimemodule.c`
  counterexample recorded, and a cross-reference added to the allocator section
  noting that *raising* and *zeroing* are separate questions.
- Agent prompts updated for each rule above. `refcount-auditor.md` gains a
  standing note mirroring `scan_init_bypass`'s canary: **check the denominator,
  not the finding count, before reporting a clean negative.**

Tests: **556 → 633.**

## [0.8.0] - 2026-07-24

The **correctness release**. A full `informed-explore` run over a 14-file
`Objects/` sample was used to audit the toolkit against real CPython source. It
found 15 FIX-class bugs — but it also found that **0 of 69 candidates from the
three largest scanners were real**, that three headline rules were dead code,
and that two entries in the false-positive taxonomy were factually wrong about
CPython. This release fixes 23 toolkit defects found that way.

Every number below was measured on CPython main @ `4f3be1b5777` (3.16.0a0).
Tests: **243 → 556**.

### Fixed — the chassis (shared; fixed upstream in cext-review-toolkit and synced)
- **`extract_functions()` silently dropped and merged functions.** CPython's
  brace-unbalanced macros (`Py_BEGIN_ALLOW_THREADS`, `Py_BEGIN_CRITICAL_SECTION`),
  the 48-name `PyObject_HEAD` punctuation family, and the `_Py_COMP_DIAG_*`
  pragma family desynchronize `tree-sitter-c`. Worst case was **misattribution,
  not omission**: `Objects/object.c` returned one record spanning **lines
  1267–3521 (2,254 lines, ~91 functions)**, so findings were confidently
  reported against the wrong function. Now: max span **126**; `dictobject.c`
  **187 → 292** functions reaching line 8569 of 8598; `Py_BEGIN_CRITICAL_SECTION`
  outside any function **19/187 → 3/187**; 3,559 → 3,751 functions tree-wide with
  **no per-file regression** and all byte offsets verified.
  New `scrub_macros()` / `parse_health()` primitives; `scrub=` on all `parse_*`.
  *Measured and rejected*: Argument Clinic substitution (would take `dictobject.c`
  to **72** functions) and ERROR-node recovery (all candidates garbage).
- **`strip_comments()` destroyed line numbers** by collapsing block comments
  without their newlines — 14 lines of drift in a single 1,070-line file. Now
  line-count-preserving, verified across all 50 `Objects/*.c`.

### Fixed — dead or structurally disabled rules
- **`scan_refcounts.py`: `borrowed-ref-across-call` did not exist.** The
  toolkit's flagship analysis was dead code (`BORROWED_REF_APIS` fed an unused
  regex). Implemented as `stale_slot_decref` + `owner_freed_before_use` with a
  `PYTHON_REACHING_APIS` table (122 → 226 entries incl. private `_Py*` aliases).
  Whole-tree volume **639 → 7**; on the sample where the old scanner scored
  **0/19** it now emits **2 findings, both ASan-confirmed bugs**.
- **`scan_error_paths.py`: an off-by-one read the return type from the line
  *above* it**, so 82% of functions had an empty type and `return_null_no_exception`
  had been evaluating ~1% of its population. `PyObject`-returning **22 → 1045**.
  Rule re-scoped to gated `alloc_null_no_memerror`; new `unconditional_pyerr_clear`.
  `Objects/` **148 → 33**, `Modules/` **458 → 61**.
- **`scan_null_checks.py`: `deref-before-check` appended nothing**, so
  `high_confidence` was permanently 0 while the agent prompt told agents to
  prioritize that empty set. Implemented properly — and it finds **exactly zero**
  on CPython main, a fact now recorded in the docstring and prompt so the zero is
  never read as an audit result. `Objects/` **113 → 1**, `Modules/` **311 → 13**.
- **`scan_init_bypass.py` saw 2 of 44 slot declarations.** `Objects/` uses the
  positional `X, /* tp_init */` form, and the marker lives in a *comment* that
  `strip_comments()` deleted. Now parsed on raw source; nullable fields
  **24 → 38**; new `addr_deref` sink; getset setters modeled.
- **`scan_memory_patterns.py` could not express its own bug shape** — no
  var-object allocator entry, and the multiply lives inside `_PyObject_VAR_SIZE`.
  New `varobject_nitems_unguarded`; GC gate made type-level; taint table split.
- **`scan_lock_discipline.py` discarded half its data file**, filtering out the
  `PyMutex` family and going blind to `weakrefobject.c`'s 16-site `LOCK_WEAKREFS`
  scheme. Both families now load and pair independently.

### Fixed — factual errors in the shipped knowledge base
- **`PyObject_Hash` was listed as recursion-guarded. It is not**
  (`Objects/object.c:1158`, unlike `PyObject_Repr` :759 / `PyObject_Str` :800 /
  `PyObject_RichCompare` :1099). An agent trusting the taxonomy would have
  dismissed the entire confirmed recursion class, both catalogued findings
  included. Corrected in `cpython_non_bugs.md` and `recursion-guard-auditor.md`.
- **The `Py_TRASHCAN` entry told agents to look for a marker that no longer
  exists** — the macros are empty backwards-compat shims with zero call sites in
  `Objects/`/`Modules/`; the live mechanism is automatic in `_Py_Dealloc`. The
  stale test biased toward *false positives*.
- **Catalog entry `OOM-0023` was mis-catalogued**, not fixed: `subtype_dealloc`
  has zero `PyErr_*` calls in 167 lines and no commit ever removed one. Removed
  with a tombstone; it was also the worked example in an agent prompt.

### Fixed — silent-wrongness in shared helpers
- `deduplicate_findings()` keyed on a *normalized* detail string that erased
  quoted names and line numbers, collapsing distinct bugs in the same file and
  hiding the second in `duplicate_locations`. Now exact on `(type, file, line)`.
- `resolve_roots()` set `scan_root = target.parent` for a file target, so
  **scanning one file silently scanned the whole directory**. Fixed there and in
  the four scanners carrying a local copy.
- `parse_common_args()` silently swallowed unknown flags; now warns on stderr.

### Fixed — history and regression tooling
- `analyze_history.py` **died on any window longer than ~10 years**
  (`text=True` with no `errors=`; one non-UTF-8 commit aborted everything). Full
  9,203-commit `Objects/` history now analyses in ~11 s. **The identical defect
  was propagated to all five sibling toolkits.**
- Unknown flags are now a hard error (`--months 420` used to run silently at the
  default 90-day window); `--max-commits` 2000 → 50000 with the cap surfaced in
  `notes[]`; `.py` dropped from discovery in a C-source toolkit.
- New `--introduced-by FILE:LINE` (validated: `genericaliasobject.c:542` →
  `1da989be74e`); crash-weighted `fix_confidence`/`crash_class` (the `fix` bucket
  was **44.9%** of commits, now 26.0%); per-file crash-fix density with
  `--follow` (ranks `genericaliasobject.c` **#1** where raw churn ranked it 36th);
  shallow-clone detection.
- `known-issues` gains **`absent_in_function`** — "the named function still
  exists and is clean" is a different signal from "the bug moved". 4 of 5
  `line_drifted` rows reclassify; `no_scanner: 0` preserved.

### Added
- **`tools/validate_precision.py`** — measures scanner volume and **line
  accuracy** (does the reported line actually carry the construct the finding
  describes?) across `Objects/`, `Modules/` and `Python/`, with baseline diffing.
- **`scan_deprecated_apis.py`** + `data/deprecated_c_apis.json` (66 verified
  entries) replacing a 2021-era pattern list that scored **0/13**; includes the
  `_Py_DEPRECATED_EXTERNALLY` tier the compiler never warns on under
  `Py_BUILD_CORE`. New `gc-untrack-macro-form` rule (2 hits tree-wide, both real).
- New FT rules `iternext_setref_null_decref` and `lazy_init_partial_guard`
  (gated on ≥2 accessors with ≥1 guarded); `Py_GIL_DISABLED` region modeling;
  positional `tp_iternext` detection. Sample precision **3/6 → 5/5**.
- `run_oom_sweep.py` gains `--setup` (arming before setup burned the budget) and
  sanitizer-aware classification — ASan's exit 1 was being read as the *safe*
  `memory_error` outcome.
- `analyze_includes.py`: directives resolved to real paths before tiering, so
  `api_tiers` and `cycles` stop being tautologies (`Objects/` internal
  **0 → 87**; edge targets matching a node key **5/1110 → 669/670**; the tree's
  one real cycle surfaced). Symbol-based fan-in alongside include fan-in.

### Changed
- `check_pep7.py`: **5,736 → 64** findings on `Objects/`. `func-call-space`
  deleted (it fired on `#define X (…)`, where removing the space changes an
  object-like macro into a function-like one); `missing-braces` and
  `line-too-long` gated behind `--diff-only` (PEP 7 says braces are required
  *"but do not add them to code you are not otherwise modifying"*); generated and
  `stringlib` headers excluded from `header-guard`. Envelope normalized to
  `findings[]`.
- `measure_c_complexity.py`: multi-line signatures and Clinic `_impl` functions
  were dropped (**+35.7%** functions recovered); hotspot threshold made relative
  (absolute `5.0` flagged **3 functions in all of `Objects/`**, max score 6.5);
  new `manual_cleanup_ladder` metric — **24 of 25 defect functions have zero
  gotos**, so in CPython a `goto` cleanup ladder is a *positive* signal.
  Documented that complexity **inverts** for the recursion class: the guard is a
  branch, so the correct twin outscores the buggy sibling.

## [0.7.0] - 2026-07-24

The dynamic-verification release. Adds the harness that turns a *static*
candidate into a *reproduced* crash, plus the two remaining static detectors
whose designs were already written down, and a differential oracle built from
CPython's own shipped dual implementations.

### Added
- **oom-reproducer** + `run_oom_sweep.py` + the **`reproduce`** command: dense
  `_testcapi.set_nomemory` OOM injection with one subprocess per iteration and
  exit-code classification (139/-11 SIGSEGV, 134/-6 SIGABRT, 1 = a clean
  `MemoryError` — the *safe* outcome). This is the technique that already found
  gh-146092 (`_PyFrame_GetLocals`) by hand. Validated against a local CPython
  build: abort detection confirmed end-to-end; the interpreter guard rejects a
  python without `_testcapi`.
- **parity-checker** + `find_parity_pairs.py`: CPython *ships* pure-Python twins
  of several C accelerators (`_pydecimal`, `_pyio`, `_pydatetime`, …), which are
  a free differential oracle — if the C side crashes where the twin raises, the
  bug is confirmed and localized. Discovery finds 39 pairs on the current tree
  (6 high-confidence).
- **init-bypass-checker** + `scan_init_bypass.py`: builds the design in
  `docs/python-wrapper-new-without-init.md` for the C side — a slot reads
  `self->field` and INCREFs/calls/derefs it with no NULL guard on a type whose
  `tp_new` doesn't guarantee initialization, or whose field is deletable
  (gh-152954, gh-152817).
- **memory-pattern-analyzer promoted to a real scanner** (`scan_memory_patterns.py`):
  integer overflow in an allocation size from a Python-controlled multiply
  (gh-3493, gh-1779) and the GC-track invariant (gh-152107); previously
  qualitative-only. The patterns the script cannot cover stay documented as an
  explicit by-hand phase.

### Changed
- `known-issues`: the `init-bypass` category is now scanned, closing the last
  `no_scanner` gap in the catalog.
- `explore` / `health` / `hotspots` wire the new agents; version → 0.7.0.

## [0.6.0] - 2026-07-24

The free-threading release. Uses the v0.5 tree-sitter chassis to add data-race
detectors for CPython's own free-threaded (`Py_GIL_DISABLED`, PEP 703) code,
grounded in the fusil `cpython-tsan-findings` catalog.

### Added
- **ft-race-scanner** + `scan_ft_races.py`: three TSan-grounded race classes —
  T3 iterator-exhaustion double-DECREF (gh-154130 / gh-144357 / gh-153296), T2
  lazy-init cache without a critical section (TSAN-0043 `descr_get_qualname`),
  T1 atomic/plain access asymmetry (TSAN-0006 `count_repr`). Suppresses the
  `*_lock_held` / `*_locked` caller-holds-the-lock convention.
- **stw-safety-checker** + `scan_stw_safety.py` (ported from ft-review-toolkit):
  flags calls inside a `_PyEval_StopTheWorld` region that can invoke Python / GC
  / set an exception, via an intra-file call graph (now possible on the chassis).
- **lock-discipline-checker** + `scan_lock_discipline.py` (ported): critical-
  section acquire/release pairing, including the `Py_BEGIN_CRITICAL_SECTION_MUTEX`
  spelling.
- **tsan-report-analyzer** + `parse_tsan_report.py` and **tsan-stress-generator**
  (ported, inverted for CPython: races in CPython's own frames ARE the target,
  not noise to filter).
- FT data files: `stw_safe_apis.json`, `lock_macros.json`,
  `critical_section_apis.json`, `atomic_patterns.json`.

### Changed
- `known-issues`: the `tsan` catalog category is now scanned by `scan_ft_races`
  (was `no_scanner` in v0.5). Only `init-bypass` remains scanner-less.
- `explore` / `hotspots` / `health` wire the free-threading agents.
- Version → 0.6.0 (both manifests).

## [0.5.0] - 2026-07-24

The chassis-and-crash-classes release. Adopts a tree-sitter-C parsing chassis
(shared with the cext/ft siblings) and adds the first crash-class detectors
grounded in the fusil OOM/TSan findings repos and the CPython tracker, plus the
informed-explore and known-issues workflow machinery.

### Added
- **Tree-sitter-C chassis**: vendored `tree_sitter_utils.py` (verbatim from the
  cext sibling) and a CPython-adapted `scan_common.py` (`find_cpython_root`,
  `resolve_roots`, `discover_c_files`, `parse_common_args`, `build_report`,
  comment-based suppression, dedup). New scanners parse real function boundaries
  instead of regex. Requires `pip install tree-sitter tree-sitter-c`.
- **recursion-guard-auditor** + `scan_recursion_guards.py`: recursion-prone slots
  (`tp_hash`/`tp_richcompare`/`tp_repr`/`tp_str`, generic-alias parameter walks)
  that descend a user-controlled object graph without `Py_EnterRecursiveCall` /
  `Py_ReprEnter` → native-stack-overflow SIGSEGV. Validates against gh-154318
  (`tuple_hash`) and gh-154275 (`_Py_make_parameters`).
- **pyerr-clear-auditor** + `scan_pyerr_clear.py`: `PyErr_Clear()` in the
  destructor family (`tp_dealloc`/`tp_clear`/`tp_finalize`/`tp_traverse`) with no
  save/restore, swallowing an in-flight `MemoryError`/`KeyboardInterrupt`.
  Validates against `deque_clear` and gh-152083 (`context_tp_dealloc`).
- **uninitialized-dealloc-auditor** + `scan_uninit_dealloc.py`: non-zeroing
  allocation freed on an error path before members are initialized → `tp_dealloc`
  reads garbage. Validates against gh-151815 (`template_iter`).
- **known-issues** command + `check_known_issues.py`: cross-references
  `data/cpython_known_bugs.tsv` (seeded from cpython-oom-findings /
  cpython-tsan-findings / the tracker) against a fresh scan.
- **informed-explore** command + `build_informed_briefing.py`: a catalog-seeded
  targeted pass driven by `data/cpython_bug_shapes.json` (guarded-twin / hunt /
  differential) and `data/cpython_non_bugs.md` (FP taxonomy), with a
  `--catalog-dir` hook into a `cpython-review-findings` repo.
- **git-history-context** preflight agent: an early per-file bug-fix-density
  watchlist (distinct from the post-hoc `git-history-analyzer`), with
  shallow-clone detection.
- **`data/` layer**: `cpython_known_bugs.tsv`, `cpython_bug_shapes.json`,
  `cpython_reachability_sources.json`, `cpython_non_bugs.md`.
- `docs/improvement-plan.md`: the v0.5+ roadmap this release begins executing.

### Changed
- `explore` command wires the three new crash-class detectors (Group A2) and adds
  the `recursion`, `pyerr-clear`, and `uninit-dealloc` aspects.
- Version → 0.5.0 across both manifests.

## [0.4.0]

Everything shipped through 0.4.0 (previously tracked under `[Unreleased]`).

### Enhanced
- `analyze_history.py`: parallelize git subprocess calls using `ThreadPoolExecutor` for ~4-8x speedup on diff extraction. Add `--workers N` option (default 8).
- `git-history-analyzer` agent: add operational guidance (unique temp filenames, long Bash timeouts, fallback on timeout).

### Added
- `git-history-analyzer` agent: fix completeness review, similar bug detection via git history, churn-risk matrix, and CPython-specific analyses (module family propagation, Argument Clinic migration completeness, API modernization gaps).
- `analyze_history.py` script: git log parsing, commit classification with CPython-extended keywords, C function boundary detection, file/function churn metrics, co-change clusters, and module family awareness.
- `init_not_reinit_safe` finding: detect tp_init functions that allocate without re-init guards.
- `new_missing_member_init` finding: detect tp_new functions using non-zeroing allocators without member initialization.
- Initial implementation of cpython-review-toolkit plugin.
- 7 analysis scripts: analyze_includes, measure_c_complexity, check_pep7, scan_refcounts, scan_error_paths, scan_null_checks, scan_gil_usage.
- 10 agent definitions: refcount-auditor, error-path-analyzer, gil-discipline-checker, c-complexity-analyzer, include-graph-mapper, pep7-style-checker, null-safety-scanner, api-deprecation-tracker, macro-hygiene-reviewer, memory-pattern-analyzer.
- 4 command definitions: explore, map, hotspots, health.
- Test helper (TempProject for C projects) and 7 test files with 61 tests.
- Plugin scaffolding: plugin.json, marketplace.json, LICENSE, .gitignore.
- Project and plugin READMEs.
