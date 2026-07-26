# ft-race-scanner — slice `obj-sequences` (Group B2, INFORMED)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** the 4 files in `preflight/slice_files.txt`
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`
**Build-matrix validity:** taken from the mapper and re-checked — `git diff a1d580430c8 4f3be1b5777 --` over the four slice files and their three clinic headers is **empty**. Every in-slice line number below is valid on every matrix build.

**Build-matrix validity for the out-of-slice files this report cites** (tasks (c) and (e) necessarily leave the slice; AGENT_BRIEF §4 rule 11 applied to each):

| file | `a1d580430c8` → `4f3be1b5777` | effect on the lines I cite |
|---|---|---|
| `Modules/arraymodule.c`, `Objects/genericaliasobject.c`, `Objects/iterobject.c`, `Objects/setobject.c`, `Modules/_elementtree.c`, `Modules/_io/{stringio,bufferedio,textio}.c` | **identical** | none |
| `Objects/unicodeobject.c` | 1 insertion / 1 deletion, **at `:14722`** (`FT_MUTEX_LOCK_FLAGS` → `FT_MUTEX_LOCK`) | none — the change is line-neutral and 200+ lines above `unicodeiter_*` |
| `Modules/_struct.c` | 4 / 22, **at `:2642-2680`** (`cache_struct_converter`) | none — my citations are `:2245`–`:2270`, above the hunk |
| `Modules/itertoolsmodule.c` | 2 / 4, **at `:3675`** | none — my citations are `:361`, `:540`, `:671-693`, `:1705-1732`, all above the hunk |

So every TSan line number quoted below is valid against the review target without adjustment, and I verified each pair's statement text in the target rather than in the build tree.

**Read first:** `AGENT_BRIEF.md` §0, `agents/include-graph-mapper.md`, `agents/gil-discipline-checker.md`, `agents/refcount-auditor.md`, `agents/git-history-context.md`, `agents/uninitialized-dealloc-auditor.md`, `agents/null-safety-scanner.md`, `preflight/informed_briefing.md`.

**Pre-run scanner:** `scan_ft_races.sample.json` — 6 findings (5×T1 low, 1×T2 medium, **0×T3**), `iternext_functions: 3`, `local_lock_wrappers: 0`, `suppressed_opaque_lock_functions: 0`, `lock_held_functions: 21`, `files_with_ft_regions: 2`.

---

## Headline

**(b) The chassis defect costs this rule exactly one finding, and I can now state it as a number rather than an estimate.** Repairing the 30 bare `*_METHODDEF` lines that break tree-sitter-c in `Objects/bytesobject.c` and re-running the *unmodified* scanner: `functions_analyzed` 331 → **371**, `iternext_functions` 3 → **4**, findings 6 → **7**. The one recovered finding is `iternext_double_decref` at `Objects/bytesobject.c:3451` (`striter_next`), **confidence `high` — the only high-confidence finding in the whole slice**, and it is CPY-0182's `next` site. The 1,141 invisible lines hold **40** functions (not 37–46), and across those 40 my three rules would have flagged **that one site and nothing else** — no T1, no T2, no publish-before-init. So the honest cost statement is: *the truncation hid 40 functions and 1 finding, but that finding was the slice's only high-confidence one, and it was the headline bug.*

**(c) `iterator_sentinel_field_asymmetry` generalises, but not in the way it was pitched, and the honest numbers are better than the pitch on yield and worse on recall.** I implemented it and ran it over **428 `.c` files** in `Objects/` + `Modules/` + `Python/`: **29 candidates, 17 true positives, 9 false positives every one of which the scanner's *existing* suppressors already handle, 3 ambiguous.** Precision 17/29 = **59% raw, 17/20 = 85%** once the existing `_lock_held` / clinic / teardown suppressors are wired in. Against the five catalogue entries the proposal named, the result splits cleanly:

| catalogue entry | rule re-finds the recorded site? | rule finds an un-found sibling? |
|---|---|---|
| CPY-0026 `ga_iternext` | **yes** | **yes** — `ga_iter_reduce:991` |
| CPY-0062 `elementiter_next` | **yes** | no — the type has no other methods (true negative) |
| CPY-0067 `arrayiter_next` | **no** | **yes** — `array_arrayiterator___reduce___impl:3289` |
| CPY-0038 `pairwise_next` | **no** | no — `pairwise` has **no method table** (true negative, verified) |
| CPY-0039 `islice_next` | **no** | no — `islice` has **no method table** (true negative, verified) |

**Recall of the recorded site is 2/5, and the miss is systematic and fixable**: three of the five iternexts load the field into a local first (`PyObject *it = lz->it; if (it == NULL)`), and step 3 only matches a test written *through* the field. That same spelling is used by `striter_next` itself, so Group B's "4/4 on this slice" is really **3 new sites + 1 that the existing T3 rule covers once discovery is fixed**. The two rules are complementary, not redundant — which is the argument for shipping both.

**The yield is real and it is not in the catalogue.** 8 accessor sites across 5 iterator types appear in **neither** findings repo: `ga_iter_reduce`, `calliter_iternext` + `calliter_reduce`, `array_arrayiterator___reduce__`, `unicodeiter_reduce` + `unicodeiter_setstate`, `iter_reduce`, `striter_reduce`/`striter_setstate` (in `cpython-review-findings` as CPY-0182 but absent from `cpython-tsan-findings`). Measured: **TSan reports a race at the predicted function/line pair for 4 of 4 probed accessors — on the marked sentinel field in 3 of them, on the adjacent cursor field in the 4th — against no TSan output at all from 3 of 3 guarded-twin controls under the identical harness.** Two of the flagged iternexts crash hard: `ga_iternext` **SIGSEGV on both FT builds**, `calliter_iternext` **SIGABRT, `object refcount : -1`, `object type name: function`** on `debug-ft-nojit` — a **novel** finding.

**(d) The D-7 lock-macro fix is completely unexercised here and I will not call it validated.** `local_lock_wrappers: 0` and `suppressed_opaque_lock_functions: 0` are both *structural* zeros: across the four `.c` files and their three clinic headers there are **35 function-like `#define`s, none of which expands to a lock primitive, and 0 SCREAMING_CASE `*LOCK*(` invocations anywhere** — so the pre-D-7 naming heuristic would have produced the identical 0. This slice cannot distinguish the old code from the new. **But it does contain the case the D-7 *principle* should be applied to next**: 16 functions in these files carry `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(x)`, i.e. they state machine-readably that the caller holds the lock, and **4 of them are not named `*_lock_held`** — `_bytearray_with_buffer:97`, `bytearray_setslice_linear:550`, `bytearray_setslice:635`, `ptr_wise_atomic_memmove:917`. The scanner's *lock-held* evidence is still name-based, exactly the thing D-7 removed for *lock macros*. Tree-wide: **112 such functions, 6 covered by nothing** the scanner has.

**(e) The CPY-0187 shape swept beyond the slice, and it is not a bytearray problem.** I implemented Group B's proposed `clinic_critical_section_coverage` gate and ran it over every Argument-Clinic file in the tree: **34 files have ≥1 `@critical_section` method; the gate yields 17 findings in 9 files**, one of which is `bytearray___init___impl` (= CPY-0187, 1/1 on its type: 33/38 locked). The other 16 are the same shape in other modules, and **five of them are `_io` types whose `__init__` is the odd one out.** I drove all five: **4 of 5 crash, 1 does not.**

**Two independent full-matrix runs (13 scenarios × 4 builds each) agree on which cells fail: 6 of 6 non-control `__init__` scenarios crash on both FT builds, 26 of 26 GIL cells are clean, and 6 of 6 controls are clean on every build.**

- **`_io.StringIO.__init__` (`Modules/_io/stringio.c:683`, 16/17 clinic impls locked)** — `mimalloc: error: double free detected of block 0x20010180000 with size 2560`, `buffer overflow in heap block … write after 40 bytes`, **SIGABRT + SIGSEGV on `debug-ft-nojit`**, SIGSEGV + two hangs on `release-ft-nojit`.
- **`_io.BufferedReader.__init__` (`Modules/_io/bufferedio.c:1591`, 25/34 locked)** — **SIGSEGV** (`Debug memory block … 15673158454381115980 bytes originally requested`) on `debug-ft-nojit`, and on `release-ft-nojit` the diagnostic that names the mechanism outright: **`Fatal Python error: PyMutex_Unlock: unlocking mutex that is not locked`** — the unlocked `__init__` overwrote the object's own lock field while a locked sibling held it.
- **`_io.BufferedWriter.__init__` (`:1943`)** — **SIGSEGV on both FT builds**.
- `_io.IncrementalNewlineDecoder.__init__` (`textio.c:247`) — **rc=0 on all builds**: its three fields are plain ints with no buffer behind them. A true negative inside the gate's own output, and the reason the gate needs a human triage step rather than being a FIX generator.

---

# 1. Task (a) — triage of the 6 pre-run findings

Verdicts up front: **2 FIX (both are CPY-0187 seen from two angles), 1 CONSIDER, 3 ACCEPTABLE.** Every one of the six is a *true* statement about the code; the two low-confidence ones that matter fired on the exact statement of a reproduced heap overflow.

## [FIX — confirms CPY-0187] F1 · T2 `lazy_init_no_critical_section` — `bytearray___init___impl:924`, `self->ob_bytes_object`

```c
924:    if (self->ob_bytes_object == NULL) {
925:        self->ob_bytes_object = Py_GetConstantBorrowed(Py_CONSTANT_EMPTY_BYTES);
926:        bytearray_reinit_from_bytes(self, 0, 0);
927:        self->ob_exports = 0;
928:    }
```

Confirmed, not re-litigated: this is the first of CPY-0187's four unsynchronised clusters (`gil-discipline-checker` G2, reproduced as `Fatal Python error: _PyMem_DebugRawRealloc: bad trailing pad byte` on `debug-ft-nojit`, 51 TSan races). The scanner's own confidence (`medium`) is **under-rated here and the reason is knowable**: T2 is medium when it finds no guarded twin *in the same file*, and there is no twin in the `.c` — the twin is the **absence** of `Py_BEGIN_CRITICAL_SECTION(self)` in `Objects/clinic/bytearrayobject.c.h:102` against its presence at 33 sibling call sites in the same header. The evidence that promotes this to high confidence lives in a file the scanner does not read. See §7.2.

One correction to the record, measured: PR #153498 (gh-153419, merged **2026-07-24**, nine days after the review ref) adds `bytearray_new` and deletes this first-init branch, so **F1 specifically is gone on today's main** while CPY-0187's `:1094-1100` cluster — F2 below — is not.

**Guarded twin:** `list___init___impl:3528-3532`, which also has no clinic critical section and is correct because it opens its own.

## [FIX — confirms CPY-0187] F2 · T1 `atomic_plain_asymmetry` — `bytearray___init___impl:1094`, `ob_alloc`, twin `bytearray_reinit_from_bytes:51`

```c
1094:        if (Py_SIZE(self) + 1 < self->ob_alloc) {     /* plain read  */
1095:            Py_SET_SIZE(self, Py_SIZE(self) + 1);     /* lost-update RMW */
1096:            PyByteArray_AS_STRING(self)[Py_SIZE(self)] = '\0';
...
1100:        PyByteArray_AS_STRING(self)[Py_SIZE(self)-1] = value;   /* OOB write */
```

`ob_alloc` has 5 accesses in the file: `FT_ATOMIC_STORE_SSIZE_RELAXED` at `:51`, `FT_ATOMIC_LOAD_SSIZE_RELAXED` at `:2538` and `:2747`, a plain read at `:218` (`bytearray_resize_lock_held`, lock held) and **the plain read at `:1094` with no lock at all**. So the asymmetry the rule reports is exactly right, and `:1094` is the **only** unsynchronised access of the five.

**This is the single most useful thing the scanner did on this slice and it is rated `low`.** `:1094` is the test guarding `:1100`, which is the statement TSan named 8 times and which the debug allocator catches as *"bad trailing pad byte"*. The rule's `low` is not a mis-calibration of *this* site — it is the rule modelling the wrong consequence: T1's text says *"the visible effect is a stale value rather than memory unsafety — one `FT_ATOMIC_*` on each access is the fix."* Here the effect is a heap buffer overflow and an atomic load is **not** the fix; the lost update on `ob_size` between `:1094` and `:1095` needs the lock. Tuning proposal in §7.1.

## [CONSIDER — true, but mis-localised] F3 · T1 `guarded_writer_unguarded_reader` — `bytearray_reinit_from_bytes:49`, `ob_start`, twin `bytearray_setslice_linear:568`

```c
47: bytearray_reinit_from_bytes(PyByteArrayObject *self, Py_ssize_t size, Py_ssize_t alloc) {
49:     self->ob_bytes = self->ob_start = PyBytes_AS_STRING(self->ob_bytes_object);
```

I enumerated all **five** call sites, which is what decides this:

| call site | caller | lock at that site |
|---|---|---|
| `:185` | `PyByteArray_FromStringAndSize` | **pre-publication** — `new` was allocated 20 lines above |
| `:285` | `bytearray_resize_lock_held` | held (asserts it at `:214`) |
| **`:926`** | **`bytearray___init___impl`** | **none** |
| **`:972`** | **`bytearray___init___impl`** | **none** |
| `:1617` | `bytearray_take_bytes_impl` | held — `clinic/bytearrayobject.c.h:665` |

So the finding is **true, and it is CPY-0187 for the third time**: `bytearray_reinit_from_bytes` is correct at 3 of its 5 call sites and unsynchronised at the two inside `__init__`. The rule blames the *callee*; the defect is in the *callers*. That is why `_caller_propagated_guards` correctly declined to whitelist it — one caller is unguarded — but the finding then names the wrong line to fix. Fixing `bytearray.__init__` fixes F1, F2 and F3 at once; "make `ob_start` atomic in `bytearray_reinit_from_bytes`" fixes none of them.

**CONSIDER rather than FIX** only because as written it points at a line that needs no change.

## [ACCEPTABLE ×3] F4/F5/F6 · T1 `atomic_plain_asymmetry` on `PyListObject.allocated` — `list_resize:107`, `list_resize:166`, `list_clear_impl:885`, twin `list_sort_impl:3190`

The dismissal needs a census, not an argument, so here it is — **every** `allocated` access reachable on the free-threaded build:

| site | function | class |
|---|---|---|
| `:46`, `:54`, `:64` | `list_allocate_array` / `list_capacity` / `free_list_items` | **`array->allocated`, a different field** — the `_PyListArray` header |
| `:107`, `:166` | `list_resize` | plain, **under the object lock** (all callers lock or are `*_lock_held`) |
| `:226` | `list_preallocate_exact` | plain, under the lock |
| `:276`, `:304` | `PyList_New` / `list_new_prealloc` | **pre-publication** |
| `:529` | `_PyList_AppendTakeRefListResize` | `assert` only |
| `:885` | `list_clear_impl` | plain, under the lock |
| `:1329`, `:1341` | `list_extend_iter_lock_held` | **`*_lock_held`** |
| `:2970`, `:2973` | `list_sort_impl` | **plain**, clinic-locked (`:2973` writes the `-1` sentinel plainly) |
| `:3172`, `:3190` | `list_sort_impl` | `:3172` plain read, `:3190` **`FT_ATOMIC_STORE_SSIZE_RELAXED`** |
| `:3528`, `:3530` | `list___init___impl` | `assert` only |
| `:3589` | `list___sizeof___impl` | **inside `#else` of `#ifdef Py_GIL_DISABLED`** — the FT arm uses `list_capacity(_Py_atomic_load_ptr(&self->ob_item))` |
| `Include/internal/pycore_list.h:42` | `_PyList_AppendTakeRef` | plain, and the header says so: *"In free-threaded build: self should be locked by the caller"* |
| `Include/cpython/listobject.h:46` | `PyList_SET_ITEM` | `assert` only |

**There is no lock-free reader of `self->allocated` on the free-threaded build.** The lock-free path reads `array->allocated` through `list_capacity()`, a *different memory location*, published atomically with `ob_item` by `_Py_atomic_store_ptr_release(&self->ob_item, &array->ob_item)` at `:165`. `self->allocated` is a redundant cache touched only under the object lock.

And the cited guarded twin is the odd one out, not the fix: **`list_sort_impl` is internally inconsistent** — it writes the `allocated = -1` sentinel *plainly* at `:2973` and restores it *atomically* at `:3190`, both under the same clinic lock. So the rule paired the file's one anomalous access against three of its eight normal ones. Not a defect; a style asymmetry with no reader that can observe it. This matches `refcount-auditor` §4's parenthetical and `gil-discipline-checker`'s §2.1 `list/allocated` row, and I add the census that makes the negative countable.

**Falsifier:** a reader of `self->allocated` (not `array->allocated`) on the FT build that does not hold `self`'s critical section. I checked all 20 sites above plus the two headers; none exists.

---

# 2. Task (b) — re-running the rule against the truncated tail, measured

## 2.1 The truncation, per file

`tree_sitter_utils.parse_health()` over the four slice files, with the count `extract_functions` actually returns:

| file | lines | functions | last function ends | coverage | ERROR nodes |
|---|---|---|---|---|---|
| `Objects/listobject.c` | 4,313 | 132 | 4,312 | 0.8129 | 11 |
| **`Objects/bytesobject.c`** | **3,926** | **67** | **2,784** | **0.5667** | **37** |
| `Objects/bytearrayobject.c` | 3,126 | 106 | 3,125 | 0.7156 | 10 |
| `Objects/bytes_methods.c` | 739 | 26 | 738 | 0.7605 | 0 |

Only `bytesobject.c` truncates. **1,141 lines (2,785–3,925) are invisible**, and the number in the envelope that shows it is `iternext_functions: 3` against four registered `tp_iternext` slots.

## 2.2 The repair, and the true counts

I repaired the parse **without touching the rule**: every line consisting solely of a bare Argument-Clinic macro name (`BYTES_LSTRIP_METHODDEF` etc.) becomes `{0},`, which is line-for-line identical in count so every reported line number stays valid against the real file. 30 such lines in `bytesobject.c`, 13 in `listobject.c`, 37 in `bytearrayobject.c`, 0 in `bytes_methods.c`. Then I ran the **unmodified** `scan_ft_races.analyze()` over the repaired tree.

| | pre-run sample | repaired | delta |
|---|---|---|---|
| `functions_analyzed` | 331 | **371** | **+40** |
| `iternext_functions` | 3 | **4** | **+1** |
| `findings` | 6 | **7** | **+1** |
| `by_confidence.high` | **0** | **1** | **+1** |
| `by_class.T3` | 0 | **1** | +1 |
| `lock_held_functions` | 21 | 21 | 0 |
| `files_with_ft_regions` | 2 | 2 | 0 |
| T1 / T2 findings | 5 / 1 | 5 / 1 | **0** |

`bytesobject.c` coverage goes 0.5667 → **0.7848**, functions 67 → **107**, last function end 2,784 → **3,925**.

The recovered finding, verbatim from the repaired envelope:

```json
{"type": "iternext_double_decref", "ft_class": "T3", "function": "striter_next",
 "member": "it_seq", "line": 3451, "confidence": "high",
 "file": "Objects/bytesobject.c"}
```

## 2.3 What else is in the 1,141 lines, and what the rules say about it

The 40 recovered functions:

```
bytes_mod  bytes_new_impl  _PyBytes_FromBuffer  _PyBytes_FromList  _PyBytes_FromTuple
_PyBytes_FromIterator  PyBytes_FromObject  bytes_alloc  bytes_subtype_new
bytes_iteritem  PyBytes_Concat  PyBytes_ConcatAndDel  _PyBytes_Resize
striter_dealloc  striter_traverse  striter_next  striter_len  striter_reduce
striter_setstate  bytes_iter  _PyBytes_RepeatBuffer
byteswriter_data  byteswriter_allocated  byteswriter_resize  byteswriter_create
PyBytesWriter_Create  _PyBytesWriter_CreateByteArray  PyBytesWriter_Discard
PyBytesWriter_FinishWithSize  PyBytesWriter_Finish  PyBytesWriter_FinishWithPointer
PyBytesWriter_GetData  PyBytesWriter_GetSize  _PyBytesWriter_GetAllocated
PyBytesWriter_Resize  _PyBytesWriter_ResizeAndUpdatePointer  PyBytesWriter_Grow
PyBytesWriter_GrowAndUpdatePointer  PyBytesWriter_WriteBytes  PyBytesWriter_Format
```

**Exactly one of the 40 produces a finding.** The reason the other 39 are silent is structural and worth recording so nobody re-derives it: 22 of them are `PyBytesWriter_*` / `byteswriter_*`, which operate on a **private, caller-owned `PyBytesWriter`** with no shared object state at all (T1 needs two accessors of one *struct field* of a shared type; T2 needs a lazy init of a self-member; T3 needs a `tp_iternext`). `_PyBytes_Resize`, `PyBytes_Concat` and the `_PyBytes_From*` family mutate a `bytes` **before publication** or under `_PyObject_IsUniquelyReferenced`, which the pre-publication suppressor and the immutability of `bytes` both cover. `striter_dealloc`/`striter_traverse` are the teardown FP class.

So the corrected denominators for this slice are:

> **`functions_analyzed: 371`, `iternext_functions: 4`, findings 7 (1 high / 1 medium / 5 low).**
> The truncation hid **40 functions (10.8% of the rule's real population) and 1 finding — but that finding was 100% of the slice's high-confidence output.**

**A second-order effect I checked and did not find.** T1 is file-scoped: it builds its field-access map from the extracted function list, so a truncated file can also lose T1 *guarded twins* and thereby suppress findings elsewhere in the same file. It did not happen here — `bytesobject.c` contributed 0 T1 findings before and after the repair, because its only atomic field (`ob_shash`, `_Py_atomic_*` at `:58`/`:71`) has no plain access anywhere in the file. Worth naming as a risk that this slice happens not to exhibit.

---

# 3. Task (c) — is `iterator_sentinel_field_asymmetry` a rule, or a slice artifact?

## 3.1 What I implemented

Group B's four steps, verbatim, in `repro/ftrace_proto_sentinel.py` (110 lines, reusing `scan_ft_races`'s own `_collect_iternext_names`, `_gil_disabled_regions`, `_in_ranges`, `strip_comments`). Step 3 is `if/while (X->F …)` followed by a **separate** `X->F` occurrence later in the same body; the receiver type is resolved from the function's own cast or parameter so `it_seq` on two different iterator types is not conflated. The same bare-macro repair from §2 is applied in memory to every file, so the measurement is about the **rule**, not about the chassis.

## 3.2 Tree-wide result: 428 files, 29 candidates

| file | n | triage |
|---|---|---|
| `Objects/bytesobject.c` | 3 | **TP** — `striter_len:3461`, `striter_reduce:3478`, `striter_setstate:3494` (= CPY-0182) |
| `Objects/unicodeobject.c` | 3 | **TP** — `unicodeiter_len:14996`, `unicodeiter_reduce:15013`, `unicodeiter_setstate:15034` |
| `Objects/iterobject.c` | 4 | **TP** — `iter_len:91`, `iter_reduce:119`, `calliter_iternext:229`, `calliter_reduce:266` |
| `Objects/genericaliasobject.c` | 2 | **TP** — `ga_iternext:942` (= CPY-0026), `ga_iter_reduce:991` |
| `Objects/setobject.c` | 1 | **TP** — `setiter_len:1062` (sibling of gh-144357 / TSAN-0054) |
| `Modules/_struct.c` | 2 | **TP** — `unpackiter_len:2245`, `unpackiter_iternext:2265` |
| `Modules/arraymodule.c` | 1 | **TP** — `array_arrayiterator___reduce___impl:3289` |
| `Modules/_elementtree.c` | 1 | **TP** — `elementiter_next:2254` (= CPY-0062) |
| `Objects/odictobject.c` | 3 | **FP** — `odictiter_nextkey_lock_held`, `odictiter_iternext_lock_held` (`*_lock_held`), `odictiter_nextkey` (asserts `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(di)` and opens `Py_BEGIN_CRITICAL_SECTION(di->di_odict)`) |
| `Modules/_csv.c` | 1 | **FP** — `Reader_iternext_lock_held` (`*_lock_held`) |
| `Modules/_io/textio.c` | 2 | **FP** — `_io_TextIOWrapper_write_impl`, `_read_impl`: clinic-`@critical_section` guarded, and `self->snapshot` is a decoder cache, not an exhaustion sentinel |
| `Modules/itertoolsmodule.c` | 2 | **FP (scope)** — `groupby_next:540`, `_grouper_next:671`: the NULL store (`gbo->currvalue = NULL`, `Py_CLEAR(gbo->currkey)`, `:692-693`) is a **per-step state reset on a value-returning path**, not an exhaustion sentinel |
| `Modules/_sqlite/cursor.c` | 4 | 1 **FP** (`cursor_clear` = `tp_clear`, teardown class) + 3 **ambiguous** (`_pysqlite_query_execute`, `pysqlite_cursor_iternext`, `pysqlite_cursor_close_impl` — sqlite3 carries its own same-thread contract via `pysqlite_check_thread`) |

**Precision: 17 TP / 29 = 59% raw.** Every one of the 9 FPs is caught by machinery `scan_ft_races` *already has* — `_caller_holds_lock` (4), `_clinic_guarded_functions` + `_has_lock` (2), the destructor-name class (1) — plus one new gate that step 1 needs anyway: **the sentinel store must be on a path that returns NULL** (2, the itertools pair). Wire those in and precision is **17/20 = 85%**, with the 3 sqlite rows the residue.

## 3.3 Recall against the five catalogue entries the proposal named

The proposal's claim was *"it generalises directly to the catalog: CPY-0067/0062/0038/0039/0026 are all recorded at `next` only. Whether any of them has a `__length_hint__`/`__reduce__` sibling is exactly the question this rule answers."* **That claim holds — the rule does answer the question for all five — but two of the five answers are "no sibling exists", and the rule also fails to re-find three of the five original sites.**

- **CPY-0038 `pairwise` and CPY-0039 `islice`: verified true negatives.** Neither type has a `PyMethodDef` table in `Modules/itertoolsmodule.c` (`grep -n "static PyMethodDef"` lists `tee`, `chain`, `product`, `combinations`, `cwr`, `permuations`, `repeat`, `module` — no `pairwise_methods`, no `islice_methods`). There is no `__reduce__`, no `__setstate__`, no `__length_hint__` to race. The rule reporting nothing is correct.
- **CPY-0062 `elementiter`: the rule re-finds the recorded site** (`:2254` tests `it->root_element`, `:2259` steals it) and finds no sibling — correct, `ElementIter_Type` is a `PyType_Spec` with only dealloc/traverse/iter/iternext slots.
- **CPY-0067 `arrayiter`: the rule misses the recorded site and finds a real sibling.** `arrayiter_next:3237` writes `arrayobject *ao = it->ao; if (ao == NULL)` — the test is on the **local**, so step 3 does not match. `array_arrayiterator___reduce___impl:3289-3292` (`if (self->ao == NULL) … return Py_BuildValue("N(O)n", func, self->ao, self->index);`) does match, and is an INCREF-of-a-possibly-freed-array — the same "opposite failure mode" TSAN-0062 documents for `dictiter_reduce`.
- **CPY-0026 `ga_iternext`: both.** The rule re-finds `:942`→`:946` (the recorded site) *and* `ga_iter_reduce:991`.

**The systematic miss is the local-alias spelling** — `T *x = obj->F; if (x == NULL) …` — used by `arrayiter_next`, `pairwise_next`, `islice_next`, `striter_next`, `unicodeiter_next`, `setiter_iternext` and `islice_next`. It is the *safer-looking* spelling and it does not change the race. **The existing T3 rule catches exactly that population** (it matches the drop, not the test), which is why the two rules should ship together: T3 covers the local-alias iternexts, `iterator_sentinel_field_asymmetry` covers the accessor siblings that T3 structurally cannot see. On this slice that partition is exact — T3 → `striter_next:3451`, sentinel-asymmetry → `striter_len`/`_reduce`/`_setstate`. **Group B's "4/4" should be read as "3 net-new + 1 already owned by T3".**

## 3.4 Cross-check against `cpython-tsan-findings`, per instruction

The catalogue **already owns the class at the `next` site**: `notes/sequence-iterator-exhaustion-double-decref.md` enumerates dict / set / str / str-ascii / bytes / tuple / list / seqiter / memoryview, with dict filed as cpython#154130 and set as cpython#144357. Two of its rows are **stale against this tree**: `listiter_next:4080` and `tupleiter_next:1147` now carry `#ifndef Py_GIL_DISABLED` around the drop (`listobject.c:4077-4081`, `tupleobject.c:1146-1149`), i.e. they are fixed; the note lists them as "same shape (not yet tripped)".

What the catalogue does **not** own is the accessor face. Grepping `catalog/known_races.tsv` + all `reports/*/meta.json`:

| accessor | in `cpython-tsan-findings`? | in `cpython-review-findings`? |
|---|---|---|
| `striter_len` | yes (TSAN-0037 signature list) | yes (CPY-0182) |
| `striter_reduce`, `striter_setstate` | **no** | yes (CPY-0182) |
| `unicodeiter_len` | yes (TSAN-0038 — but recorded as the **`it_index` cursor** face only) | no |
| `unicodeiter_reduce`, `unicodeiter_setstate` | **no** | **no** |
| `iter_len` | yes (TSAN-0044 — again `it_index` only) | no |
| `iter_reduce` | **no** | **no** |
| `calliter_iternext`, `calliter_reduce` | **no** | **no** |
| `ga_iter_reduce` | **no** | **no** |
| `array_arrayiterator___reduce__` | **no** | **no** |
| `setiter_len` | yes (TSAN-0040/0054) | no |
| `unpackiter_len`, `unpackiter_iternext` | yes (TSAN-0039) | no |

**8 sites across 5 types are new to both repositories.**

## 3.5 Measured — `repro/ftrace_sentinel_accessor_family.py`

One drain thread calling `next()`, 7 probe threads calling **only** the accessor under test, one scenario per subprocess, 4,000 rounds × 12 calls, `PYTHON_GIL=0` on the FT builds.

### Hard crashes (the two flagged *iternexts*)

| scenario | site | `debug-ft-nojit` | `release-ft-nojit` | `debug-gil-nojit` |
|---|---|---|---|---|
| `ga_next` | `ga_iternext:942/952` | **SIGSEGV (rc=139)** | **SIGSEGV (rc=139)** | rc=0 |
| `calliter_next` | `calliter_iternext:229/243` | **SIGABRT (rc=134)** | rc=0 | rc=0 |

`calliter_next`, verbatim — **this is novel**:
```
Python/gc_free_threading.c:1083: validate_refcounts: Assertion "_Py_REFCNT(op) > 0" failed
object refcount : -1
object type name: function
Fatal Python error: _PyObject_AssertFailed
```
`iter(callable, sentinel)` → `calliterobject`; `calliter_iternext:243/244` does `Py_CLEAR(it->it_callable); Py_CLEAR(it->it_sentinel);` guarded only by the plain `if (it->it_callable == NULL)` at `:229`. Two threads both pass the gate and both drop the callable. **`Objects/iterobject.c` has 0 critical sections and 0 `FT_ATOMIC_*`.**

### The accessors: TSan, with the guarded-twin control

`release-ft-nojit-tsan`, 400 rounds, same thread mix. Every process **exited** (rc=66) before I read its log — the partial-log trap is in the taxonomy and I checked for it.

| scenario | warnings | unique | the pair TSan reports | field |
|---|---|---|---|---|
| `ga_reduce` | 1 | 1 | `Write ga_iternext:952` ↔ `Read ga_iter_reduce:991` | **`gi->obj`** — `Py_SETREF(gi->obj, NULL)` vs `if (gi->obj)`. **The predicted sentinel pair.** |
| `calliter_reduce` | 3 | 1 | `Read calliter_reduce:266` ↔ `Write calliter_iternext:243` | **`it->it_callable`** — `if (it->it_callable != NULL && …)` vs `Py_CLEAR(it->it_callable)`. **Predicted.** |
| `str_reduce` | 3 | 1 | `Read unicodeiter_reduce:15013` ↔ `Write unicode_ascii_iter_next:14986` | **`it->it_seq`** — `Py_BuildValue("N(O)n", iter, it->it_seq, it->it_index)` vs `it->it_seq = NULL`. **Predicted.** |
| `array_reduce` | 3 | 1 | `Read array_arrayiterator___reduce___impl:3292` ↔ `Write arrayiter_next:3245` | **`it->index`** — the **cursor**, not the `it->ao` sentinel. `:3245` is `return (*it->getitem)(ao, it->index++);`. Same accessor pair, different field. |
| **`ctl_list_reduce`** | **0 — no log file produced at all** | — | — | — |
| **`ctl_tuple_reduce`** | **0 — no log file** | — | — | — |
| **`ctl_bytearray_reduce`** | **0 — no log file** | — | — | — |

**4 of 4 flagged accessors produce a race at exactly the function/line pair the rule predicts; 3 of 3 guarded twins produce no TSan output at all under the identical harness.** That differential is the evidence for the rule.

**One honest deduction.** Only **3 of the 4** landed on the field the rule marked. `array_reduce`'s race is on `it->index` (the cursor) rather than `it->ao` (the sentinel) — same two functions, same accessor, adjacent field. That is a *weaker* confirmation than the other three: it proves the accessor is unsynchronised against the iternext, not that the specific sentinel window was hit. The `it->ao` window at `:3289`→`:3292` remains static-only. `arraymodule.c` has **0** critical sections and **0** `FT_ATOMIC_*` in the whole file, so both fields are equally unsynchronised.

**The one thing I will not overstate.** All three race addresses are in the `0x7fffb6……` range and TSan emitted no `Location is heap block` attribution, which is the signature the FP taxonomy flags as possible stack-slot reuse. I am reporting these as **corroborating, not conclusive**: what makes them believable is (i) the function/line pair is exactly the predicted one in all three cases, and (ii) the twin control is silent. Falsifier: run the same three under `debug-ft-nojit-tsan` and check for a `Location is heap block of size N` line naming the iterator object.

The pure-accessor scenarios did **not** hard-crash at 4,000 rounds on any build — consistent with TSAN-0062's own note that *"the window between the NULL check and the deref/INCREF is a few instructions"*. Workload controls (`solo_bytes_reduce`, `solo_ga_reduce`, `solo_str_reduce`, one thread, same call volume) rc=0 everywhere, so the variable is concurrency.

## 3.6 Verdict on the proposal

**Ship it, with three amendments, and do not ship it as a replacement for T3.**

1. **Also match the local-alias spelling** — `T *x = obj->F;` … `if (x == NULL)` … `obj->F` — or state explicitly that T3 owns that half. Without this the rule's recall on the recorded catalogue sites is 2/5.
2. **Gate step 1 on the NULL store being on a NULL-returning path.** That is the whole difference between an exhaustion sentinel and `_grouper_next`'s per-step reset, and it removes the only FP class the existing suppressors don't already cover.
3. **Reuse the existing suppressors.** `_caller_holds_lock`, `_clinic_guarded_functions`, `_has_lock` and the destructor-name class take precision from 59% to 85% with no new logic.

Fixtures: positives `Objects/bytesobject.c:3434-3502`, `Objects/genericaliasobject.c:930-995`, `Objects/iterobject.c:224-270`; negatives `Objects/listobject.c:4065-4123`, `Objects/tupleobject.c:1130-1160`, `Objects/bytearrayobject.c:2974-3062`, and — for the suppressors — `Objects/odictobject.c:1740-1830`.

---

# 4. Task (d) — the D-7 lock-macro change is unexercised here. Stated plainly.

`local_lock_wrappers: 0`, `suppressed_opaque_lock_functions: 0`, `local_lock_macro_names: []`, `files_with_local_lock_macros: 0`. **These are structural zeros and this slice cannot validate the change.** Measured, over the four `.c` files *and* their three clinic headers:

| file | function-like `#define`s | any expanding to a lock primitive? | SCREAMING_CASE `*LOCK*(` invocations |
|---|---|---|---|
| `Objects/listobject.c` | 5 (`b`, `ISLT`, `IFLT`, `MAX_MERGE_PENDING`, `MERGE_GETMEM`) | **no** | **0** |
| `Objects/bytesobject.c` | 7 (`CHARACTER`, `EMPTY`, `WRITE_BYTES_LEN`, …) | **no** | **0** |
| `Objects/bytearrayobject.c` | 4 (`PyByteArray_SIZE_MAX`, `STRINGLIB`, …) | **no** | **0** |
| `Objects/bytes_methods.c` | 2 (`STRINGLIB`, `ADJUST_INDICES`) | **no** | **0** |
| the 3 `clinic/*.c.h` | 17 (`KWTUPLE` ×16, `KWTUPLE`) | **no** | **0** |

Because there are **0** `*LOCK*(` invocations, `_LOCK_MACRO_RE` never fires either — so the **pre-D-7 naming heuristic would have produced the identical `0`/`0`**. There is no observable difference between the old and new code on this slice. Not "validated"; not "regressed"; **not exercised**.

## 4.1 A case in these four files that *should* exercise the same principle — and does not

D-7's insight is *stop identifying a lock by its name; identify it by what it expands to.* The rule still identifies **"the caller holds the lock"** purely by name — `_caller_holds_lock()` tests for the `_lock_held` / `_locked` / `_LockHeld` suffix. Meanwhile CPython states that fact machine-readably, in these very files, **16 times**:

| file | functions asserting `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` | of which **not** named `*_lock_held` |
|---|---|---|
| `Objects/bytearrayobject.c` | 14 | **3** — `_bytearray_with_buffer:97`, `bytearray_setslice_linear:550`, `bytearray_setslice:635` |
| `Objects/listobject.c` | 2 | **1** — `ptr_wise_atomic_memmove:917` |
| **total** | **16** | **4 (25%)** |

Three of those four are load-bearing for findings in this slice: `bytearray_setslice_linear:568` is the guarded twin the scanner cites in F3, and `bytearray_setslice:635` is the gh-153578 vehicle.

**How much would it buy?** I measured it rather than asserting it. Over `Objects/` + `Modules/` + `Python/`, **112** functions assert the object is locked:

| already covered by | count |
|---|---|
| `_caller_holds_lock` (name suffix) | 53 |
| clinic `@critical_section` | 0 |
| `_has_lock` (takes a lock itself) | 4 |
| `_caller_propagated_guards` (transitive call-site propagation) | **49** |
| **nothing** | **6** |

The 6: `Objects/setobject.c:3151 _PySet_NextEntryRef` (called from `listobject.c:1386`, i.e. inside this slice's include graph) and five `Modules/_ctypes/_ctypes.c` `*_paramfunc` functions.

**Proposal (small, and honest about its size):** seed `_caller_propagated_guards`'s `guarded` set from `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` / `_Py_CRITICAL_SECTION_ASSERT_MUTEX_LOCKED` in the body, exactly as D-7 seeds the lock-macro set from `#define` bodies. **Expected effect: up to 6 fewer potential FPs tree-wide, 0 on this slice** — most of the population is already rescued by propagation, which is the honest reason this is a robustness improvement rather than a bug fix. It also makes the rule independent of a naming convention that CPython does not enforce; `_bytearray_with_buffer` is the in-slice proof that the convention is not universal.

---

# 5. Task (e) — new territory: sweeping the CPY-0187 shape beyond bytearray

## 5.1 Framing, using Group B's measured semantics rather than re-deriving them

I take as given (G3, measured): a per-object critical section **is** released on `_PyThreadState_Detach` → `_PyCriticalSection_SuspendAll`, so it excludes across a *busy* user callback (1.200 s) and not across a *blocking* one (0.000 s). That settles the RMW question I would otherwise have had to test: **an `ob_exports++` / `ob_size` RMW performed under the object's own critical section is still serialised** — suspend/resume brackets the whole region, it does not interleave the RMW. So the RMW hazard lives entirely at sites with **no lock at all**, which is what CPY-0187 is.

## 5.2 The public C-API surface — a real negative with its denominator

Group B's denominator was *clinic entry points* (36 bytearray + 14 list) and *protocol slots* (14 bytearray). The surface they did not bound is the **public C API** in these files, which C extensions call directly. I swept every `Py(List|Bytes|ByteArray)_*` / `_Py*_*` function in the three object files for a direct write to `ob_size` / `ob_item` / `allocated` / `ob_start` / `ob_alloc` / `ob_exports` / `ob_bytes_object`:

| function | writes | verdict |
|---|---|---|
| `PyList_New:240` | `ob_item`, `allocated`, `Py_SET_SIZE` ×3 | **pre-publication** |
| `PyList_SetItem:453` | `FT_ATOMIC_STORE_PTR_RELEASE` | `Py_BEGIN_CRITICAL_SECTION` |
| `_PyList_AppendTakeRefListResize:525` | `FT_ATOMIC_STORE_PTR_RELEASE` | caller-locked, **documented** in `pycore_list.h:36` |
| `_PyList_AsTupleAndClear:3274` | `Py_SET_SIZE` | `Py_BEGIN_CRITICAL_SECTION` |
| `PyByteArray_FromStringAndSize:153` | `ob_bytes_object`, `ob_exports` | **pre-publication** |
| `Objects/bytesobject.c` | — | **0 sites**: `bytes` is immutable after publication |

**4 public-API mutators of shared state in the slice, all synchronised or pre-publication. Clean, and the denominator is 4.**

## 5.3 The productive sweep: Group B's `clinic_critical_section_coverage`, measured tree-wide

CPY-0187 is not findable at any single site — the signal is that **33 of 38 siblings lock and one does not**. I implemented that gate (`repro/ftrace_clinic_cs_coverage.py`): for every `.c` with a sibling `clinic/*.c.h` containing ≥1 `Py_BEGIN_CRITICAL_SECTION` around an `_impl` call, report the impls the wrapper does **not** lock, keeping only those whose body **directly** writes a field of its first parameter (so delegation to a locking helper is excluded — the `bytearray.clear` case).

**34 files qualify. The gate produces 17 findings in 9 files.** The full result:

| file | locked / total clinic impls | unguarded impls that write self's fields |
|---|---|---|
| `Modules/_remote_debugging/module.c` | 7/27 | `RemoteUnwinder___init___impl:335` (26 fields), `BinaryReader.__init__/__exit__/close`, `BinaryWriter.__init__/__exit__/close` |
| `Modules/_io/bufferedio.c` | 25/34 | `BufferedReader___init___impl:1591`, `BufferedWriter___init___impl:1943`, `BufferedRandom___init___impl:2484`, `BufferedRWPair___init___impl:2273` |
| **`Objects/bytearrayobject.c`** | **33/38** | **`bytearray___init___impl:914` — CPY-0187, and nothing else** |
| `Modules/_io/stringio.c` | 16/17 | `_io_StringIO___init___impl:683` |
| `Modules/_io/textio.c` | 26/34 | `IncrementalNewlineDecoder___init___impl:247` |
| `Modules/_multiprocessing/semaphore.c` | 5/11 | `SemLock__after_fork_impl:681` |
| `Modules/_asynciomodule.c` | 36/57 | `Task___init___impl:2299` |
| `Modules/_lsprof.c` | 9/10 | `profiler_init_impl:1008` |

26 further files — including `Objects/listobject.c` (8/14), `Objects/dictobject.c`, `Objects/setobject.c`, `Modules/_io/bytesio.c` (20/20), `Modules/_ssl.c` (88/92), `Modules/_collectionsmodule.c` — produce **zero** findings. `Objects/bytearrayobject.c` yields **exactly one, and it is the reproduced bug**: 1/1 precision on the type this slice already settled.

**The shape is a `__init__` problem, not a bytearray problem.** 13 of the 17 are `__init__`; the rest are `__exit__` / `close` / `_after_fork`. The mechanism is the same every time: `__init__` is an ordinary method on an already-published object, callable again from Python at any time, and the sweep that added `@critical_section` to the type's methods skipped it because no stress script calls it — the *exact* gh-129107/PR #129108 story `gil-discipline-checker` documented for bytearray, repeated in five other modules.

## 5.4 Reproduced — `repro/ftrace_init_unlocked_siblings.py`

4 threads, one shared already-published object, 3,000 rounds × 16 ops, **one scenario per subprocess**. Exit codes are the process's own, not a pipeline's.

**Two independent full-matrix runs**, 13 scenarios × 4 builds each. They agree exactly on *which* cells fail; the *signal* varies run to run, as a race should.

| scenario | site | `debug-ft-nojit` | `release-ft-nojit` | `debug-gil` | `release-gil` |
|---|---|---|---|---|---|
| `sio_init_vs_write` | `_io_StringIO___init___impl:683` vs locked `write` | **134 SIGABRT** / **139 SIGSEGV** | **139 SIGSEGV** / **124 hang** | 0 / 0 | 0 / 0 |
| `sio_init_vs_init` | two unlocked `__init__` | **134** / **134** | **124** / **124** | 0 / 0 | 0 / 0 |
| `sio_init_vs_read` | `__init__` vs locked `getvalue` | **139** / **134** | **124** / **124** | 0 / 0 | 0 / 0 |
| **`sio_write_only`** | **CONTROL — locked sibling only** | **0 / 0** | **0 / 0** | **0 / 0** | **0 / 0** |
| `br_init_vs_read` | `_io_BufferedReader___init___impl:1591` vs locked `read` | **139** / **139** | **134** / **134** | 0 / 0 | 0 / 0 |
| `br_init_vs_init` | two unlocked `__init__` | **139** / **139** | **124** / **124** | 0 / 0 | 0 / 0 |
| **`br_read_only`** | **CONTROL** | **0 / 0** | **0 / 0** | **0 / 0** | **0 / 0** |
| `bw_init_vs_write` | `_io_BufferedWriter___init___impl:1943` vs locked `write` | **139** / **139** | **139** / **134** | 0 / 0 | 0 / 0 |
| **`bw_write_only`** | **CONTROL** | **0 / 0** | **0 / 0** | **0 / 0** | **0 / 0** |
| `nld_init_vs_decode` | `IncrementalNewlineDecoder___init___impl:247` | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| **`nld_decode_only`** | **CONTROL** | **0 / 0** | **0 / 0** | **0 / 0** | **0 / 0** |
| **`solo_sio_init`** | **CONTROL — single-threaded** | **0 / 0** | **0 / 0** | **0 / 0** | **0 / 0** |
| **`solo_br_init`** | **CONTROL — single-threaded** | **0 / 0** | **0 / 0** | **0 / 0** | **0 / 0** |

**6 of 6 non-control `__init__` scenarios fail on both FT builds in both runs — 12 crashing cells per run. 0 failures across `debug-gil-nojit` and `release-gil-nojit`: 26/26 clean, twice. Every one of the 6 control cells is rc=0 on every build in both runs.**

Crash text, verbatim, `debug-ft-nojit`:
```
mimalloc: error: thread 0x795f39ffd6c0: double free detected of block 0x20010180000 with size 2560
mimalloc: error: thread 0x765ff2ffe6c0: buffer overflow in heap block 0x200041d0090 of size 40: write after 40 bytes
mimalloc: assertion failed: at "Objects/mimalloc/alloc.c":278, mi_page_usable_size_of   assertion: "ok"
Objects/unicodeobject.c:677: _PyUnicode_CheckConsistency
Debug memory block at address p=0x20008240010: API '.'
    15673158454381115980 bytes originally requested
```
and on `release-ft-nojit`, `br_init_vs_read` — the line that names the mechanism:
```
Fatal Python error: PyMutex_Unlock: unlocking mutex that is not locked
Python runtime state: initialized
```
`BufferedObject` carries its own `PyThread_type_lock`/`PyMutex`; the unlocked `__init__` reassigns it while a locked sibling is inside it.

**Double free, heap buffer overflow, corrupted debug-allocator headers and an unlocked-mutex fatal error — from pure Python, no `_testcapi`, no OOM injection, with locked-sibling and single-threaded controls clean on every build and both GIL build families clean on every scenario.** This is CPY-0187's failure mode, in `_io`, three times.

**The one candidate that did not crash is worth as much as the four that did.** `IncrementalNewlineDecoder.__init__` writes `pendingcr`, `seennl` and `translate` — three plain `int`s with no buffer behind them, so a lost update corrupts decoder state and nothing else. The gate is a *candidate* generator; the discriminator a human still has to apply is whether the unlocked writes reach a pointer or a size that indexes memory.

Out of slice, so I am **not** recording it as a slice finding — I am reporting it as the measured answer to "sweep for the same shape elsewhere", and as the strongest available evidence that `clinic_critical_section_coverage` deserves to ship. Filing is the campaign's call.

## 5.5 Prior art

`gh api -X GET search/issues -f q='repo:python/cpython …'` for `StringIO __init__ free-threading critical section`, `BufferedReader __init__ thread safety free threaded`, `_io __init__ critical_section race`, `bytes iterator __reduce__ thread`, `callable_iterator free-threading`, `generic alias iterator __reduce__ race` returns the gh-124397 iterator umbrella, gh-129107/#129108 (bytearray — the sweep that missed `__init__`), gh-153928 (the str-iterator cursor race, = TSAN-0038) and gh-149816 (the 22-race umbrella). **Nothing on any `__init__` in `_io`, nothing on `calliter`, nothing on `ga_iter_reduce`.** All merge dates checked against the 2026-07-15 ref; the only in-family merge after it is PR #153498 (2026-07-24), noted in §1.

---

# 6. Classes bounded — with the corrected denominators

| class | denominator | verdict |
|---|---|---|
| **T3 iterator owning-ref drop** | **4** `tp_iternext` slots in the slice (**not 3** — `iternext_functions` is corrected in §2.2) | **1 broken** (`striter_next:3451`, = CPY-0182); 3 guarded (`listiter_next:4078`, `listreviter_next:4237`, `bytearrayiter_next:2997`), each by the `#ifndef Py_GIL_DISABLED` elision + an `FT_ATOMIC` index |
| **T2 lazy init of a self-member** | 1 site in the slice | **1 finding**, = CPY-0187 cluster 1; removed on main by PR #153498 |
| **T1 field synchronisation asymmetry** | 4 findings on 2 fields | **1 FIX** (`ob_alloc:1094`, = CPY-0187), **3 ACCEPTABLE** (`allocated`, census in §1: 20 sites, **0 unsynchronised readers on the FT build**) |
| **T1 `guarded_writer_unguarded_reader`** | 1 finding | **CONSIDER** — true, mis-localised; 5 call sites enumerated, 2 unguarded, both in `__init__` |
| **`bytesobject.c` truncated tail** | **40 functions** over 1,141 lines | **1 finding** (`striter_next`); 22 are `PyBytesWriter_*` on a private struct, the rest pre-publication or immutable-`bytes` |
| **Local lock-wrapper macros (D-7)** | **35 function-like `#define`s** across 7 files (4 `.c` + 3 `clinic/*.c.h`); **0** `*LOCK*(` invocations | **structural zero — the change is UNEXERCISED, not validated** |
| **`_Py_CRITICAL_SECTION_ASSERT_*` as lock evidence** | **16** functions in-slice; **112** tree-wide | **4 in-slice / 6 tree-wide** are invisible to every mechanism the scanner has |
| **Public C-API mutators of shared state in the slice** | **4** (`PyList_New`, `PyList_SetItem`, `_PyList_AppendTakeRefListResize`, `_PyList_AsTupleAndClear`, `PyByteArray_FromStringAndSize`) | **clean** — locked, caller-locked-by-contract, or pre-publication. `bytesobject.c` contributes 0: immutable after publication |
| **`iterator_sentinel_field_asymmetry`, tree-wide** | **428 `.c` files**, 29 candidates | **17 TP / 9 FP / 3 ambiguous**; 59% raw, **85%** with the existing suppressors |
| **`clinic_critical_section_coverage`, tree-wide** | **34 files** with ≥1 `@critical_section` | **17 findings in 9 files**; `bytearrayobject.c` 1/1 = CPY-0187; 26 files clean including `listobject.c` (8/14), `dictobject.c`, `setobject.c`, `bytesio.c` (20/20), `_ssl.c` (88/92) |
| **Of those 17, the 5 `_io` candidates driven dynamically** | **13 scenarios × 4 builds × 2 independent runs = 104 cells** | **4 of 5 candidates crash** (`StringIO.__init__`, `BufferedReader.__init__`, `BufferedWriter.__init__`): 6/6 non-control FT scenarios fail in both runs. **1 clean** (`IncrementalNewlineDecoder.__init__`, int-only fields). **26/26 GIL cells clean, twice; 6/6 controls clean on every build, twice** |
| **Files with no per-object locking at all, among the types this report touches** | grep over each file | `Objects/bytesobject.c` **0/0**, `Objects/iterobject.c` **0/0**, `Modules/arraymodule.c` **0/0** critical sections / `FT_ATOMIC_*`. Each hosts a flagged iterator; the file-level zero is the structural explanation |

**Deliberately not re-litigated:** CPY-0180 (`strip` UAF), CPY-0182 (`striter_*` — I confirm the `next` site is recoverable by T3 and quantify the accessor family), CPY-0184/0185/0186, CPY-0187 (three of my six findings are it), CPY-0014, gh-153570, gh-153578, gh-153419/#153498, gh-124397, TSAN-0037/0038/0039/0040/0044/0053/0054/0062.

---

# 7. Toolkit assessment

## 7.1 [HIGH VALUE] T1's confidence is attached to the wrong consequence

`atomic_plain_asymmetry` fired at `bytearrayobject.c:1094` — the guard expression of a **reproduced heap buffer overflow** — with `confidence: "low"` and a detail string that says *"Py_ssize_t does not tear on LP64, so the visible effect is a stale value rather than memory unsafety — one `FT_ATOMIC_*` on each access is the fix."* Both halves are wrong at this site: the effect is a lost-update RMW that drives `ob_size` past `ob_alloc`, and an atomic load does not fix it.

The discriminator is mechanical and needs no new analysis:

> **Promote a T1 site from `low` to `high` when the plain access is part of a read-modify-write of the receiver's own size/capacity state** — the same field appears on both sides of an assignment within one statement or two adjacent ones (`Py_SET_SIZE(self, Py_SIZE(self) + 1)`, `self->F++`, `if (Py_SIZE(self) + 1 < self->ob_alloc)` immediately followed by `Py_SET_SIZE`) — **and** the enclosing function is not covered by any lock. Change the detail text for that case: the fix is a critical section, not an atomic.

Measured on this slice: **1 promotion (`:1094`), 0 false promotions** — `list_resize:107`/`:166` and `list_clear_impl:885` are all under the object lock, so the "not covered by any lock" clause excludes them. This is the cheapest change in this report and it converts the slice's headline bug from a `low` to a `high`.

## 7.2 [HIGH VALUE] I third the parse-health canary, and I can now price it

`refcount-auditor` §6.1 and `uninitialized-dealloc-auditor` §5.2 both proposed it; `gil-discipline-checker` §6.2 seconded it. §2 above is the price tag **for this rule specifically**: 40 hidden functions, 1 hidden finding, and that finding is 100% of the slice's high-confidence output. The envelope already had the tell — `iternext_functions: 3` for a slice with four registered `tp_iternext` slots — and nothing surfaced it.

Two concrete asks, in priority order:

1. **Emit `parse_health()` into the envelope on every scan, and add a `notes[]` entry when `coverage < 0.65`.** `tree_sitter_utils.parse_health()` already computes exactly the number that would have caught this (0.5667) and almost nothing calls it. Cost: three lines in `analyze()`.
2. **Make `extract_functions` error-tolerant at the top level** — on a top-level `ERROR` node, skip it and keep walking siblings. The repair in §2 shows what is recoverable: `bytesobject.c` goes 67 → 107 functions, coverage 0.5667 → 0.7848, from neutralising 30 lines. Regression fixture: `Objects/bytesobject.c:2787-2843` verbatim. This is the **second** instance of the family (`extract_functions` skipping `preproc_ifdef`, fixed as cext #64 / ft #24).

**One rule-specific datum for whoever implements it:** `_collect_iternext_names()` finds `striter_next` correctly today — it reads the slot table with a regex over the whole source, and the positional-slot pattern matches `Objects/bytesobject.c:3541`. It is only *function discovery* that drops it, so `_is_iternext` is never called. Fixing discovery is therefore **sufficient** for T3 here; no rule change is needed, and a regression test asserting `striter_next` produces `iternext_double_decref` would lock it in.

## 7.3 [HIGH VALUE] Ship `iterator_sentinel_field_asymmetry`, with §3.6's three amendments

Tree-wide numbers in §3.2/§3.3. The two decisive facts for a reviewer: **precision 85% after reusing suppressors the scanner already has**, and **8 sites across 5 iterator types that appear in neither findings repository**, three of which I corroborated with TSan against a silent guarded-twin control. It does **not** replace T3 — the two rules partition the population by *spelling* (test-through-the-field vs test-through-a-local), and each is blind to the other's half.

## 7.4 [HIGH VALUE] Ship `clinic_critical_section_coverage` — §5.3 is its tree-wide validation

Group B proposed it from 2 types in 1 slice. Measured over 34 files: **17 gated findings in 9 files, 26 files clean, and 1/1 precision on the type where the answer is already known.** The `__init__` concentration (13 of 17) is a finding about CPython, not about the rule. It also **fixes the 41-region clinic blind spot as a side effect** — you cannot compute the ratio without reading `clinic/*.c.h`, which is the thing four agents in this slice have now independently asked for.

One calibration note for whoever triages its output: the FP taxonomy's entry on gh-127192 (*concurrent `__init__`/construction is a different concern from normal method races*) will be raised against every one of these. The counter is Group B's and it is measured: `__init__` on an **already-published** object is an ordinary method call, and `sio_write_only` — the locked sibling, same harness, same rounds — is clean on every build while `sio_init_vs_write` double-frees.

## 7.5 [MEDIUM] Seed the guard set from `_Py_CRITICAL_SECTION_ASSERT_*` — D-7's principle, one heuristic over

§4.1. **6 functions tree-wide, 0 in this slice.** Small, and I am pricing it honestly rather than dressing it up: 49 of the 112 are already rescued by `_caller_propagated_guards`. The argument for doing it anyway is the same one that justified D-7 — a naming convention CPython does not enforce (`_bytearray_with_buffer` is the in-slice counter-example) should not be the only evidence a rule accepts when the source states the fact explicitly.

## 7.6 [LOW] Two envelope fixes

- **`iternext_functions` is a denominator that can silently under-report.** It is the *only* signal that a file's iterator population was not seen, and on this slice it read `3` against a true `4` with nothing to flag it. Pair it with the count of `tp_iternext` slot names `_collect_iternext_names()` resolved from the source (which was correct at 4): a mismatch between "slots found in the source" and "iternext functions extracted" is a one-line, zero-cost truncation canary that is *specific to this rule*.
- **`local_lock_macro_names: []` and `files_with_local_lock_macros: 0` cannot be told apart from "the rule did not run".** When both are zero, emit the count of function-like `#define`s examined (35 here across 7 files). That is the difference between "no lock macros exist" and "I did not look", and §4 needed a separate script to establish it.

## 7.7 What the informed method bought, and what it cost

**Bought.** The whole of §3 and §5 is fix-propagation on shapes other agents had already localised: I did not discover the striter family or CPY-0187, I turned each into a rule and measured the rule on 428 files. `ga_iter_reduce`, `calliter_*`, `_io.StringIO.__init__` and `_io.BufferedReader.__init__` all fell out of that, and none of them is in my slice — they exist because the slice's two confirmed shapes were sharp enough to generalise. The tsan-findings cross-check also **stopped** me publishing the `unicodeiter_*` family as novel: the class is `notes/sequence-iterator-exhaustion-double-decref.md`, and the honest contribution is the *accessor* face plus two stale rows in that note.

**Cost, twice, both mine.** (i) I built the prototype's step 3 to Group B's letter and only noticed afterwards that it cannot match `striter_next` — the very site the proposal cites as its first true positive. Reading the four catalogue iternexts *before* implementing would have surfaced the local-alias spelling in ten minutes. (ii) My first matrix driver decoded child stderr as strict UTF-8 and died on `0xdd` — the debug allocator's freed-memory fill — losing a run. AGENT_BRIEF's non-UTF-8 rule exists for exactly this and I applied it to the wrong layer.

---

# 8. What I did not do

- **The TSan accessor evidence is corroborating, not conclusive.** All four race addresses are in the `0x7fffb6……` range with no `Location is heap block` attribution, which the FP taxonomy flags as possible stack-slot reuse. What carries it is the exact function/line pair in 4 of 4 and the silent twin control in 3 of 3. Falsifier: `debug-ft-nojit-tsan` with a heap-attributed location naming the iterator object.
- **`array_reduce`'s TSan pair is on the cursor, not the sentinel.** The `self->ao` window at `arraymodule.c:3289`→`:3292` is still static-only; I did not construct a probe that forces it.
- **I did not hard-crash any pure accessor.** 4,000 rounds × 8 threads on both FT builds; consistent with TSAN-0062's measured note that the window is a few instructions. The two hard crashes (`ga_next`, `calliter_next`) are on the *iternext*, not the accessor.
- **I did not check the 3 `Modules/_sqlite/cursor.c` candidates against sqlite3's `check_same_thread` contract.** Left ambiguous in §3.2 rather than counted as either TP or FP.
- **I did not run the `_remote_debugging`, `_asyncio.Task`, `_lsprof` or `_multiprocessing.SemLock` candidates from §5.3.** Only the five `_io` ones (4 crash, 1 clean). The other 12 gated findings are static.
- **I did not isolate which unlocked write produces which `_io` crash.** The `sio_*` scenarios drive the whole of `_io_StringIO___init___impl:683`, which writes 12 fields including `buf`, `string_size` and `pos`; the debug-allocator and mimalloc diagnostics are consistent with the buffer reallocation but I built no single-field variant. Same for `BufferedReader`.
- **I did not run the `_io` scenarios on `release-gil-nojit-asan`.** `debug-ft-nojit`'s mimalloc already reports the double free by name, and FT ASan has no object-heap shadow.
- **I did not build today's `main`.** The PR #153498 claim about F1 is read from `gh api`, second-hand from `gil-discipline-checker`'s fetch.
- **I did not file anything upstream**, and I did not add records to either findings repository — both are the campaign's call.
- **`Objects/bytes_methods.c` contributes nothing to this rule and that is structural**: no per-object state, no locks, no atomics, 0 `tp_iternext`. It receives `(const char *, Py_ssize_t)`. Zero sites in every sweep here.
- **No JIT builds, and no `release-gil-nojit-asan` run for §5.4** — the debug-FT mimalloc diagnostics are already a hard double-free with the right controls, and FT ASan has no object-heap shadow (AGENT_BRIEF §4.6).

---

# 9. Artifacts

Reproducers, all `.py` files, one scenario per subprocess, no heredocs — `reports/obj-sequences/repro/`:

- **`ftrace_sentinel_accessor_family.py`** — task (c). 23 scenarios: 13 flagged accessor/iternext sites across 7 iterator types, 7 guarded-twin controls (list / tuple / bytearray), 3 single-threaded workload controls.
- **`ftrace_init_unlocked_siblings.py`** — task (e). 13 scenarios over the five `_io` candidates from the `clinic_critical_section_coverage` sweep, with locked-sibling and single-threaded controls for each type.
- **`ftrace_matrix_driver.py`** — build-matrix driver; one subprocess per cell, `PYTHON_GIL=0` per build family, and it decodes child output with `errors="replace"` so a crashing child's freed-heap bytes cannot kill the run.

Analysis scripts — copied into `reports/obj-sequences/repro/` so they outlive the session scratchpad; each reproduces the numbered result next to it:

- **`ftrace_measure_truncation.py`** — §2.1, the per-file `parse_health` table
- **`ftrace_repair_and_rescan.py`** — §2.2, the bare-macro repair + **unmodified** `scan_ft_races.analyze()` re-run (331→371, 3→4, 6→7)
- **`ftrace_proto_sentinel.py`** — §3, the `iterator_sentinel_field_asymmetry` prototype (428 files → 29 candidates)
- **`ftrace_check_lockmacros.py`**, **`ftrace_assert_locked_treewide.py`** — §4 (35 `#define`s / 0 lock-shaped; 112 assert-locked functions, 6 uncovered)
- **`ftrace_clinic_cs_coverage.py`** — §5.3 (34 files → 17 gated findings in 9)

Also in the session scratchpad, not copied: `assert_locked_fns.py` (the in-slice 16/4 split), `rmw_sweep.py` and `list_entrypoints.py` (§5.2's public-API and slot tables), `list_tail.py` (the 40 tail functions).

TSan logs: `/tmp/tsan_sent_{ga_reduce,calliter_reduce,str_reduce,array_reduce}.<pid>`; controls produced no log file at all.

---

## Summary line

```
FIX=2  CONSIDER=1  ACCEPTABLE=3          (all 6 pre-run findings triaged; 3 of them are CPY-0187)
CORRECTED DENOMINATORS: functions_analyzed 331 -> 371 ; iternext_functions 3 -> 4 ; findings 6 -> 7
  truncation cost = 40 functions, 1 finding -- and that finding was the slice's ONLY high-confidence one
iterator_sentinel_field_asymmetry: 428 files, 29 candidates, 17 TP / 9 FP / 3 ambiguous
  = 59% raw, 85% with the scanner's EXISTING suppressors; recall of the 5 named catalogue sites 2/5
  (systematic miss = the local-alias spelling, which T3 already owns -- ship BOTH)
  8 accessor sites across 5 iterator types are in NEITHER findings repo
  TSan: predicted function/line pair 4/4 (marked field 3/4, adjacent cursor 1/4); twin controls 0 output 3/3
D-7 (discover_local_lock_macros): UNEXERCISED here -- 35 #defines, 0 lock-shaped, 0 *LOCK*( invocations;
  the pre-D-7 heuristic gives the identical 0. Same principle, one heuristic over: 4 in-slice / 6 tree-wide
  functions state "caller holds the lock" via _Py_CRITICAL_SECTION_ASSERT and no mechanism reads it
clinic_critical_section_coverage swept tree-wide: 34 files, 17 findings in 9; bytearray 1/1 = CPY-0187;
  REPRODUCED the same shape in _io -- StringIO.__init__ (double free + heap overflow), BufferedReader.__init__
  (SIGSEGV + "PyMutex_Unlock: unlocking mutex that is not locked"), BufferedWriter.__init__ (SIGSEGV):
  6/6 non-control FT scenarios fail in TWO independent full-matrix runs; 26/26 GIL cells clean twice;
  6/6 controls clean on every build twice; 1 of 5 candidates (IncrementalNewlineDecoder) clean --
  int-only fields, the gate's own true negative
novel out-of-slice crash: calliter_iternext (iter(callable, sentinel)) refcount -1, SIGABRT on debug-ft
```
