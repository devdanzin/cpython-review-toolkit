# ft-race-scanner — `Modules/` sample (informed)

Toolkit **v0.8.0**, `scan_ft_races.py`. Target `/home/danzin/projects/cpython` @ `4f3be1b5777`
(3.16.0a0). Repro builds `/home/danzin/projects/python_build_matrix/builds/debug-ft-nojit-{asan,tsan}`
@ **`a1d580430c8`**.

> **Build/HEAD divergence — the build tree is NEWER than the scanned HEAD.**
> `git merge-base --is-ancestor 4f3be1b5777 a1d580430c8` → YES. `a1d580430c8` (2026-07-18) is
> **3 days ahead** of the scanned HEAD `4f3be1b5777` (2026-07-15). Byte-identity of every file
> I cite:
>
> | file | HEAD vs build |
> |---|---|
> | `Modules/_elementtree.c` `_pickle.c` `_collectionsmodule.c` `_csv.c` `_datetimemodule.c` | **identical** |
> | `Modules/_struct.c` | differs only in `cache_struct_converter` (:2642+); **`unpackiter_iternext` :2255–2280 identical** |
> | `Modules/itertoolsmodule.c` | differs only in `count_repr` (:3675–3681); **every other cited function identical**, and `count_nextlong:3639` is identical |
> | `Modules/arraymodule.c` | **identical** |
>
> The one itertools delta is itself a finding — see FIX-6.

## Scanner volume

```
raw (sample): 16 | confirmed FIX: 5 | CONSIDER: 4 | ACCEPTABLE/FP: 7 | precision(FIX): 31%
raw (Modules/): 58 | denominators: iternext_functions 40, lock_held_functions 81, files_with_ft_regions 36
```

| type | raw | FIX | CONSIDER | FP | precision (FIX) |
|---|---|---|---|---|---|
| **T3** `iternext_double_decref` | 6 | 5 | 0 | 1 | **83 %** |
| **T2** `lazy_init_no_critical_section` | 5 | 0 | 3 | 2 | **0 %** (60 % actionable) |
| **T1** `atomic_plain_asymmetry` | 5 | 0 | 0 | 5 | **0 %** |
| `lazy_init_partial_guard` | **0** anywhere in 380 files | — | — | — | zero yield |

Two of the three highest-value findings below (**FIX-6**, **CON-1**) were **found by reading, not by
the scanner** — and in both cases the scanner reported the *safe* accessor of the same field and
suppressed the dangerous one. See Toolkit assessment.

## Findings

### FIX — reproduced crashes

Every crash below was produced on `debug-ft-nojit-asan` with `PYTHON_GIL=0`. Control: the identical
driver on `debug-gil-nojit-asan` **survived 60 000 trials, exit 0** — these are free-threaded-build-only.

---

#### [FIX-1] `Modules/_struct.c:2270` · `unpackiter_iternext` · SIGSEGV

```c
if (self->so == NULL) {                    /* :2265  guard */
    return NULL;
}
if (self->index >= self->buf.len) {
    Py_CLEAR(self->so);                    /* :2270  owning drop */
    PyBuffer_Release(&self->buf);          /* :2271  buffer release */
    return NULL;
}
...
self->index += self->so->s_size;           /* :2278  so re-read AFTER the drop */
```

**What breaks.** The `so == NULL` guard and the `Py_CLEAR(self->so)` are not atomic. Thread B passes
the guard, thread A exhausts and clears; B then dereferences `self->so->s_size` at :2278 on a NULL
pointer. Three further consequences are static-only but follow from the same window: a
double-`Py_CLEAR` (refcount underflow on the `Struct`), a **double `PyBuffer_Release`** (the export
count of the underlying `bytearray`/`memoryview` decremented twice, plus a double `Py_DECREF` of
`buf.obj`), and a UAF read of `self->buf.buf` inside `s_unpack_internal` after B released it.

**Guarded twin.** None in-file — `Modules/_struct.c` contains **zero** `Py_BEGIN_CRITICAL_SECTION` /
`FT_ATOMIC` / `PyMutex` tokens. The twin is cross-file: `Modules/_csv.c:999 Reader_iternext` →
`Reader_iternext_lock_held`, and `Modules/_collectionsmodule.c:1995 dequeiter_next` →
`dequeiter_next_lock_held` under `Py_BEGIN_CRITICAL_SECTION2(it, deque)`.

**Python reach.** `it = struct.iter_unpack('i', memoryview(bytearray(4)))`, `next(it)` from ≥2 threads.

```
==4073238==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000020 (pc 0x700ec2162df9 ... T1)
==4073238==The signal is caused by a READ memory access.
==4073238==Hint: address points to the zero page.
    #0 0x700ec2162df9 in unpackiter_iternext .../Modules/_struct.c:2278
    #1 0x644ee0b561de in builtin_next .../Python/bltinmodule.c:1776:11
    #2 0x644ee0b64669 in _Py_BuiltinCallFast_StackRef .../Python/ceval.c:817:11
SUMMARY: AddressSanitizer: SEGV .../Modules/_struct.c:2278 in unpackiter_iternext
```
`0x20` == `offsetof(PyStructObject, s_size)` from NULL. Reproduced **3 of 4 runs**, exit 1.

**Fix.** Wrap the body in `Py_BEGIN_CRITICAL_SECTION(op)`.

---

#### [FIX-2] `Modules/_elementtree.c:2259` · `elementiter_next` · SIGABRT (negative refcount)

```c
if (!it->root_element) {                    /* :2254 */
    PyErr_SetNone(PyExc_StopIteration);
    return NULL;
}
elem = it->root_element;  /* steals a reference */   /* :2259 */
it->root_element = NULL;                             /* :2260 */
```

**What breaks.** Two threads both read the same non-NULL `root_element`, both "steal" it, both NULL
the slot — the single owning reference is handed to two owners, and the second `Py_DECREF` drives the
`Element` to a negative refcount. `it->parent_stack_used` and `parent_stack_push_new`'s
`PyMem_Resize(it->parent_stack, ...)` are equally unsynchronised, so the same window also allows two
concurrent reallocs of one buffer (static observation).

**Guarded twin.** None in-file — `Modules/_elementtree.c` has **zero** FT primitives. Cross-file twin
as in FIX-1.

**Python reach.** `it = iter(xml.etree.ElementTree.Element('root'))`, `next(it)` from ≥2 threads.

```
./Include/internal/pycore_stackref.h:726: _Py_NegativeRefcount: Assertion failed: object has negative ref count
<object at 0x7b4c74cbc330 is freed>
Fatal Python error: _PyObject_AssertFailed: _PyObject_AssertFailed
```
Exit **134**, reproduced **2/2**, at trial 0 (immediate).

**Fix.** `Py_BEGIN_CRITICAL_SECTION(op)` over the `while (1)` body.

---

#### [FIX-3] `Modules/itertoolsmodule.c:361` · `pairwise_next` · SIGABRT (refcount −1)

`Py_CLEAR(po->it)` at :361 and :373, `Py_CLEAR(po->old)` at :366 and :374, with the `it == NULL`
guard at :354 and a `tp_iternext` call in between. `itertools.pairwise([])` puts every thread's
*first* `next()` on the drop path.

**Guarded twin — in the same file, eight of them.** `chain_next` (:2009), `product_next` (:2276),
`combinations_next` (:2514), `cwr_next` (:2760), `permutations_next` (:3040), `accumulate_next`
(:3189), `zip_longest_next` (:4032) and `tee_next` (:979) all wrap their body in
`Py_BEGIN_CRITICAL_SECTION(op)`. `batched_next` (:223/:233/:241) and `cycle_next` (:1296) use the
*other* canonical fix — the drop elided under `#ifndef Py_GIL_DISABLED` plus `FT_ATOMIC_*` on the
exhaustion flag. **12 of itertools' 21 `tp_iternext` slots are hardened; `pairwise`, `groupby`,
`_grouper`, `islice` are not.** The 2024–2026 sweep stopped short of exactly these four.

```
Python/gc_free_threading.c:1083: validate_refcounts: Assertion "_Py_REFCNT(op) > 0" failed:
tracked objects must have a reference count > 0
object refcount : -1
```
Exit **134**, crashed at trial 10 000–20 000 of 60 000 with 8 threads.

---

#### [FIX-4] `Modules/itertoolsmodule.c:1732` · `islice_next` · SIGABRT (refcount −1)

`empty: Py_CLEAR(lz->it);` at :1732, guarded only by `if (it == NULL) return NULL;` at :1705.
`itertools.islice(iter([]), 4)`.

```
Python/gc_free_threading.c:1083: validate_refcounts: Assertion "_Py_REFCNT(op) > 0" failed
object refcount : -1
object type name: list_iterator
```
Exit **134**, first trial batch. Same guarded twins as FIX-3. Note this is a *separate fix* from
FIX-3 per the "report each `tp_iternext` separately" rule.

---

#### [FIX-5] `Modules/itertoolsmodule.c:691` · `_grouper_next` · SIGABRT (assertion)

```c
r = gbo->currvalue;          /* :691  steal */
gbo->currvalue = NULL;       /* :692 */
Py_CLEAR(gbo->currkey);      /* :693 */
```

`_grouper_next` mutates its **parent groupby's** state with no lock on either object. Two threads
both steal `currvalue`, and `Py_CLEAR(gbo->currkey)` races the `assert(gbo->currkey != NULL)` at
:676 and `groupby_step`'s `Py_XSETREF(gbo->currkey, newkey)`.

**Python reach.** `gb = itertools.groupby([1,1]); _, g = next(gb)`, then `next(g)` from ≥2 threads.

```
python: ./Modules/itertoolsmodule.c:676: PyObject *_grouper_next(PyObject *):
        Assertion `gbo->currkey != NULL' failed.
```
Exit **134**, ~5 000–10 000 trials, 4 threads. (With `NDEBUG` this is a NULL passed to
`PyObject_RichCompareBool`.)

---

#### [FIX-6] `Modules/itertoolsmodule.c:3678/3691/3696` · `count_repr` · **gh-153908 is an incomplete fix** · TSan race + SIGSEGV

**Not from the scanner** — found by reading the diff between HEAD and the repro build.

`5200f119242` "gh-153908: Fix data race in `itertools.count.__repr__` (GH-153917)" landed *between*
the scanned HEAD and the build tree. It converted the `lz->cnt` read to
`FT_ATOMIC_LOAD_SSIZE_RELAXED` and **left all three plain reads of `lz->long_cnt` / `lz->long_step`
alone**, while `count_nextlong:3639` writes `lz->long_cnt = stepped_up;` under
`Py_BEGIN_CRITICAL_SECTION(lz)` (taken by `count_next:3662`). Same shape as gh-153298
(`ga_parameters` / CPY-0025) and gh-128714 (`func.__annotations__` / CPY-0029): **guarded writer,
unguarded reader of the same field.**

`count_repr` does not merely *read* the pointer — it hands the borrowed value to
`PyUnicode_FromFormat("%s(%R)", …, lz->long_cnt)`, so the writer can free the `int` under
`PyObject_Repr`.

**Python reach.** `c = itertools.count(2**70)` (slow mode: `cnt == PY_SSIZE_T_MAX`,
`long_cnt != NULL`), then `next(c)` in one thread and `repr(c)` in another.

```
WARNING: ThreadSanitizer: data race (pid=4099264)
  Write of size 8 at 0x7ceec8a967b8 by thread T2:
    #0 count_nextlong .../Modules/itertoolsmodule.c:3639:18
    #1 count_next     .../Modules/itertoolsmodule.c:3663:24
    #2 builtin_next   .../Python/bltinmodule.c:1776:11
  Previous read of size 8 at 0x7ceec8a967b8 by thread T6:
    #0 count_repr     .../Modules/itertoolsmodule.c:3678:13
    #1 PyObject_Repr  .../Objects/object.c:784:11
    #2 builtin_repr   .../Python/bltinmodule.c:2677:12
SUMMARY: ThreadSanitizer: data race .../Modules/itertoolsmodule.c:3639:20 in count_nextlong
```
(build line `:3678` is `if (lz->long_cnt == NULL) {`, textually identical to HEAD `:3678`.)

The same run produced the downstream use-after-free — **10 TSan warnings and a hard SEGV**:

```
      4 SUMMARY: ThreadSanitizer: data race Objects/longobject.c:2177:20 in long_to_decimal_string_internal
      3 SUMMARY: ThreadSanitizer: data race ./Include/object.h:234:16 in _Py_TYPE_impl
      2 SUMMARY: ThreadSanitizer: data race ./Include/internal/pycore_long.h:254:40 in _PyLong_DigitCount
      1 SUMMARY: ThreadSanitizer: data race ./Modules/itertoolsmodule.c:3639:18 in count_nextlong
      1 SUMMARY: ThreadSanitizer: SEGV ... in __tsan_read8

==4099264==ERROR: ThreadSanitizer: SEGV on unknown address (pc 0x56102735db6c ... T4099273)
==4099264==The signal is caused by a READ memory access.
    #0 __tsan_read8
    #1 PyObject_Repr            .../Objects/object.c:766:21
    #2 unicode_fromformat_arg   .../Objects/unicodeobject.c:2966:16
    #3 unicode_from_format      .../Objects/unicodeobject.c:3085:17
    #6 count_repr               .../Modules/itertoolsmodule.c:3691:20
```
Exit **66**. On the scanned HEAD **both** halves are still plain, so HEAD is strictly worse.

**Fix.** Take `Py_BEGIN_CRITICAL_SECTION(lz)` in `count_repr` — atomics cannot fix this one, because
the borrowed `long_cnt` must stay alive across `PyObject_Repr`.

---

### CONSIDER

#### [CON-1] `Modules/_collectionsmodule.c:1986` vs `:2049` · `it->counter` · reproduced TSan race

**Not from the scanner** (it reported `dequeiter_new:2035` instead — see ACC-1).
`dequeiter_next_lock_held:1986` writes `it->counter--` plainly *inside*
`Py_BEGIN_CRITICAL_SECTION2(it, deque)`; `dequeiter_len:2049` reads it with
`FT_ATOMIC_LOAD_SSIZE(it->counter)` **outside any critical section**. An atomic read racing a
non-atomic write is a race by definition, and the atomic spelling is proof the maintainers knew the
field was shared.

```
WARNING: ThreadSanitizer: data race (pid=4101411)
  Write of size 8 at 0x7fffb672ebb0 by thread T2:
    #0 dequeiter_next_lock_held .../Modules/_collectionsmodule.c:1986:16
    #1 dequeiter_next           .../Modules/_collectionsmodule.c:2004:14
    #2 builtin_next             .../Python/bltinmodule.c:1776:11
  Previous atomic read of size 8 at 0x7fffb672ebb0 by thread T4:
    #0 _Py_atomic_load_ssize    ./Include/cpython/pyatomic_gcc.h:296:10
    #1 dequeiter_len            .../Modules/_collectionsmodule.c:2049:22
SUMMARY: ThreadSanitizer: data race .../Modules/_collectionsmodule.c:1986:16 in dequeiter_next_lock_held
```
`operator.length_hint(it)` concurrent with `next(it)`. Exit 66. **CONSIDER, not FIX**: `Py_ssize_t`
does not tear on LP64, so the visible effect is a stale `__length_hint__`, not memory unsafety.
Fix is one line — `FT_ATOMIC_STORE_SSIZE_RELAXED(it->counter, it->counter - 1)` at :1986 (and the
same at `dequereviter_next_lock_held:2137`, plus the plain reads at :1974/:1979/:1987/:2066/
:2123/:2127/:2138/:2185).

#### [CON-2] `Modules/arraymodule.c:3247` · `arrayiter_next` · **scope escape, reproduced SIGABRT**

Outside the 12-file sample (the whole-`Modules/` run flagged it). Classic T3 spelling:
```c
it->ao = NULL;        /* :3247 */
Py_DECREF(ao);        /* :3248 */
```
guarded only by `if (ao == NULL) return NULL;` at :3238. No FT primitives in the file.

```
./Modules/arraymodule.c:3248: _Py_NegativeRefcount: Assertion failed: object has negative ref count
```
`iter(array.array('i'))`, `next(it)` × 8 threads, exit **134**. Filed as CONSIDER only because it is
outside the assigned scope; on the merits it is a FIX identical to FIX-1/2.

#### [CON-3] `Modules/_sqlite/cursor.c:1164` · `pysqlite_cursor_iternext` · scope escape, static only

`Py_CLEAR(self->statement)` with no critical section; the `self->statement == NULL` guard is at
:1144. `sqlite3` has its own `check_thread` discipline, so sharing one cursor across threads may
already be rejected upstream of this line — I did not verify. Static only.

#### [CON-4] `Modules/_pickle.c` · `_Unpickler_MemoPut:1636` (`memo_dict`), `fast_save_enter:2188`
(`fast_memo`), `_pickle_Pickler___init___impl:5059` (`memo`)

All three are genuine `if (self->f == NULL) self->f = compute();` shapes with no lock. But the honest
framing is module-level, not site-level: **`Modules/_pickle.c` has 3 `Py_BEGIN_CRITICAL_SECTION`s in
8 298 lines** (`:2062` on a `modules` dict, `:3560`/`:3683` on a user `obj`) and **no per-object
locking on `Pickler`/`Unpickler` state at all**. There is no guarded twin to point at, so these are
three grains on an unprotected beach; reporting them as three FIXes would misstate the problem.
Reachability is also thin — `fast_save_enter` needs `pickler.fast` *and* 50-deep nesting;
`Pickler.__init__` needs a concurrent re-`__init__`. POLICY-adjacent: *decide* whether `_pickle`
declares FT-safety, then fix wholesale.

---

### ACCEPTABLE / false positives

| # | site | why not a bug |
|---|---|---|
| ACC-1 | `_collectionsmodule.c:2035` `dequeiter_new` (T1 `counter`) | **Pre-publication read, and the source says so verbatim**: `/* It's safe to read directly from it without acquiring the per-object lock; the iterator isn't visible to any other threads yet. */` |
| ACC-2 | `_csv.c:1804` `csv_exec` (T1 `field_limit`) | `module_state->field_limit = 128 * 1024;` in the module-exec function, before the module object escapes. The two live accesses (`:716`, `:1699`, `:1710`) are already `FT_ATOMIC_*`. |
| ACC-3 | `_datetimemodule.c:912` `set_date_fields` (T1 `hashcode`) | `self->hashcode = -1;` in a constructor helper called immediately after `tp_alloc`. Every one of the six such sites (`:912 :1334 :1405 :3227 :4691 :5468`) is post-alloc pre-publication; every *live* access (`:2564 :3974 :5099 :6670` and their stores) is already `FT_ATOMIC_*`. |
| ACC-4 | `itertoolsmodule.c:1247` `itertools_cycle_impl` (T1 `index`) | `lz->index = -1;` immediately after `type->tp_alloc`, before `return (PyObject *)lz`. |
| ACC-5 | `itertoolsmodule.c:1710` `islice_next` (T1 `cnt`) | **Struct name collision.** `isliceobject.cnt` (`:1585`) is a different field from `countobject.cnt` (`:3479`). islice's `cnt` is never touched atomically anywhere. The rule matched a member *name* file-globally. |
| ACC-6 | `itertoolsmodule.c:3622` `count_nextlong` (T2 `long_cnt`) | Its **only** caller, `count_next:3662`, wraps it in `Py_BEGIN_CRITICAL_SECTION(lz)`. This is the `*_lock_held` convention without the naming convention. (The *unguarded* accessor of the same field is `count_repr` — FIX-6 — which the scanner did not report.) |
| ACC-7 | `_pickle.c:886` `_PyMemoTable_ResizeTable` (T2 `mt_table`) | Not a lazy init. `self->mt_table = PyMem_NEW(...); if (self->mt_table == NULL) { self->mt_table = oldtable; PyErr_NoMemory(); return -1; }` — an **allocation-failure rollback** restoring a saved local. |
| ACC-8 | `itertoolsmodule.c:537` `groupby_next` (T3 `currgrouper`) | `const void *currgrouper;  /* borrowed reference */` (`:432`). `gbo->currgrouper = NULL` drops nothing; `groupby_traverse`/`groupby_dealloc` do not touch it. Not the double-DECREF shape. (It *is* a plain write racing the plain read at `:669` — wrong results, not memory unsafety.) |

## New siblings of known shapes

1. **`iternext_double_decref` (TSAN-0053 dict / TSAN-0054 set / CPY-0026 `ga_iternext`) — six new
   `Modules/` siblings, five reproduced.** `unpackiter_iternext`, `elementiter_next`,
   `pairwise_next`, `islice_next`, `_grouper_next` (+ `arrayiter_next` as a scope escape). The
   run-context hypothesis holds: the upstream sweep followed *named containers* and skipped the
   stdlib accelerators. Inside `itertoolsmodule.c` the split is measurable — 12 of 21 `tp_iternext`
   slots hardened, 4 with real drops left alone.
2. **guarded-writer / unguarded-reader (CPY-0025 `ga_getitem`, CPY-0029 `PyFunction_GetAnnotations`)
   — one new sibling, and it is a *seven-day-old incomplete fix*.** `count_repr` vs
   `count_nextlong` (FIX-6). gh-153908 fixed the `cnt` half and left `long_cnt`; the residual half
   is the one that reaches `PyObject_Repr` on a borrowed pointer and SEGVs.
3. **atomic-read / plain-write (TSAN-0006) — one new sibling.** `dequeiter_len:2049` vs
   `dequeiter_next_lock_held:1986` (CON-1). Reproduced under TSan.

TSAN-0006 `count_repr` itself: **confirmed still live on the scanned HEAD** (`:3680` reads `lz->cnt`
plainly), **partially fixed** at `a1d580430c8` by `5200f119242`. Not re-litigated.

## Classes bounded (clean negatives, with evidence)

- **`_collectionsmodule.c` T3: clean.** Both `tp_iternext` slots (`dequeiter_next:2083`,
  `dequereviter_next:2201`) delegate to a `*_lock_held` helper under
  `Py_BEGIN_CRITICAL_SECTION2(it, deque)`. The `*_lock_held` suppression convention is **correct
  here** — verified by reading both call sites. No over-suppression found in the sample.
- **`_csv.c` T3: clean.** `Reader_iternext:999` → `Reader_iternext_lock_held:925` under a critical
  section.
- **`_json.c`, `_heapqmodule.c`, `_queuemodule.c`, `_zoneinfo.c`, `_randommodule.c`, `_pickle.c`:
  T3 vacuously clean** — `grep -c tp_iternext` = 0 in all six.
- **`itertoolsmodule.c` — the five *other* unguarded iternexts are not T3.** `dropwhile_next`,
  `takewhile_next`, `starmap_next`, `compress_next`, `filterfalse_next` never drop `lz->it`; they
  race only `int` flags (`lz->start`, `lz->stop`). Data corruption at worst, no memory unsafety, and
  the scanner correctly did not flag them — real precision evidence.
- **iternext extraction recall is fine on `Modules/`.** `iternext_functions: 40` against 36 distinct
  designated/`PyType_Slot` targets plus a handful of positional forms (all in `_testcapi*`,
  `xxsubtype`, `_testbuffer`, and `_datetimemodule.c`'s `0, /* tp_iternext */` placeholders). No
  `Objects/bytesobject.c striter_next`-style extraction hole in this scope.

## Toolkit assessment

### Precision per rule (sample, n=16)

| rule | raw | TP | precision | note |
|---|---|---|---|---|
| T3 `iternext_double_decref` | 6 | 5 | **83 %** | the flagship; 5/5 reproduced as hard crashes |
| T2 `lazy_init_no_critical_section` | 5 | 0 FIX / 3 CONSIDER | **0 % / 60 %** | 2 mechanical FPs; the 3 survivors are all in one un-hardened module |
| T2 `lazy_init_partial_guard` | **0** | — | — | **zero yield across all 380 files** |
| T1 `atomic_plain_asymmetry` | 5 | **0** | **0 %** | 4 pre-publication writes + 1 struct-name collision |

T3 earns its "high" confidence and then some. T1 as currently written is pure noise on `Modules/` —
and, worse, *anti*-signal: in both files where a genuine T1 exists (`itertoolsmodule.c` `cnt`,
`_collectionsmodule.c` `counter`) the rule reported the **safe** access and hid the dangerous one.

### Recall gaps found by reading

1. **Dedup by normalized detail string erases function and line.** The FP taxonomy already warns
   about this (`xibufferview_dealloc`); here it costs true positives twice.
   - `itertoolsmodule.c` field `cnt`: 2 candidate sites, identical detail text → 1 survives. The
     survivor is `islice_next:1710` (an unrelated struct, ACC-5); the casualty is
     `count_repr:3680` — **TSAN-0006, the run's own calibration entry**.
   - `_collectionsmodule.c` field `counter`: 13 plain accesses (`:1934 :1974 :1979 :1986 :1987
     :2035 :2066 :2113 :2123 :2127 :2137 :2138 :2185`) → 1 reported, and the reported one is the
     provably-safe `dequeiter_new:2035`.
2. **T1 matches member *names* file-globally with no struct resolution** (ACC-5). The taxonomy
   already has this lesson under a different rule ("a sibling type's macro is not this type's
   macro"); T1 needs it too.
3. **The pre-publication / initializer suppression is not applied to T1.** All four remaining T1
   FPs are constructor or module-exec writes. `_INITIALIZER_NAME_RE` *already matches*
   `dequeiter_new` — so the suppression exists and simply isn't wired into the T1 path.
4. **"Caller holds the section" is modelled only by the `*_lock_held` name.** `count_nextlong`
   (ACC-6) has exactly one caller, three lines away, holding `Py_BEGIN_CRITICAL_SECTION(lz)`. A
   one-hop intra-file caller check would remove it.
5. **T2's lazy-init regex matches allocation-failure rollback** (ACC-7):
   `x->f = ALLOC(); if (x->f == NULL) { x->f = saved; ... return -1; }`.
6. **`lazy_init_partial_guard` looks for the wrong polarity, and yields nothing.** It requires ≥2
   accessors *both in the lazy-init shape*. But every catalogued instance of this bug —
   CPY-0025 (`ga_getitem`), CPY-0029 (`PyFunction_GetAnnotations`), and now gh-153908
   (`count_repr`) — is **guarded writer vs unguarded reader of the same field**, where the writer
   need not be a lazy init at all. Zero findings in 380 files is the symptom.

### Prompt problems

- The agent prompt's suppression list says `*_lock_held` callees are suppressed "because the caller
  holds the section", but does not say the *converse* is unmodelled — I nearly dismissed
  `count_nextlong` as a scanner bug rather than as a missing rule. Worth one sentence.
- The prompt tells me to compare `grep -c tp_iternext` against `iternext_functions`. On `Modules/`
  the raw grep (72) is ~2× the real slot count (36 distinct targets) because it counts comments,
  placeholders, and forward declarations. The check should be against *distinct designated/spec
  targets*, or it manufactures a false alarm.

### Ranked tuning proposals

1. **Make `deduplicate_findings` key on `(file, type, function, line)` for `scan_ft_races`, not on
   the normalized detail string.** Highest impact by a wide margin: it is single-handedly
   responsible for losing TSAN-0006 *and* the deque race, i.e. both of this run's reader-found
   findings. It is also a *shared* helper, so the same loss is happening in the other scanners
   (already documented for `scan_pyerr_clear`). If a global change is too risky, add a
   `dedup_key=` parameter and pass it here.

2. **Retarget `lazy_init_partial_guard` at "guarded writer / unguarded reader of the same field",
   and fold T1 into it.** Concretely: for each struct member `f` in a file, collect every access;
   if ≥1 access is *synchronised* (inside `Py_BEGIN_CRITICAL_SECTION` / `PyMutex` / `_Py_atomic_*` /
   `FT_ATOMIC_*` / a `*_lock_held` callee) and ≥1 is a plain access **that is not
   pre-publication**, emit one finding **per unsynchronised site**, carrying the guarded twin's
   `function:line`. Rank a plain *pointer* read above a plain *scalar* read — the pointer case is
   the one that becomes a use-after-free (`count_repr` SEGV) rather than a stale integer
   (`dequeiter_len`). This one change converts T1 from 0 % precision to the rule that would have
   found FIX-6 and CON-1 by itself, and gives `lazy_init_partial_guard` a non-empty domain.

3. **Wire the existing pre-publication suppression into T1, and widen its trigger from the function
   name to the flow.** Suppress a plain access when (a) the enclosing function name matches
   `_INITIALIZER_NAME_RE` — which already covers `dequeiter_new` — or (b) the receiver is a local
   assigned from `tp_alloc` / `PyObject_*_New` / `PyType_GenericAlloc` earlier in the same function
   and not yet returned/stored (covers `set_date_fields` via its caller, `itertools_cycle_impl`), or
   (c) the function is a module-exec (`*_exec`, `PyInit_*`) or the write is a constant/sentinel store
   (`= -1`, `= 0`, `= NULL`) with no prior read (covers `csv_exec`). This kills 4 of the 5 T1 FPs.

*Also worth doing, lower value:*

4. **Resolve the member name to its struct** before pairing atomic and plain accesses (ACC-5). If
   full type resolution is too much, a cheap proxy works: require the *receiver variable* to have the
   same declared type at both sites (`countobject *lz` vs `isliceobject *lz`).
5. **One-hop caller check for T2/T3:** if every intra-file caller of the flagged function wraps the
   call in a critical section, suppress (ACC-6). Cheap — the file's call graph is already needed by
   `scan_stw_safety.py`.
6. **Exclude allocation-failure rollback from the T2 lazy-init pattern** (ACC-7): if the value
   assigned inside the NULL check is a *local variable* (rather than a call), it is a restore, not an
   init.
7. **Add an "owning reference" gate to T3** (ACC-8): require the member to be visited by the type's
   `tp_traverse` or `Py_XDECREF`'d in its `tp_dealloc`. `groupby.currgrouper` is in neither and is
   explicitly commented `/* borrowed reference */`. This is the only T3 FP in the sample and would
   push T3 to 100 %.

### Suppressions honoured

`lock_held_functions: 81` on `Modules/`. Spot-verified at all four sample sites
(`dequeiter_next_lock_held`, `dequereviter_next_lock_held`, `Reader_iternext_lock_held`, and
`itertools`' `teedataobject_*`): the caller does hold the section in every case. **No
over-suppression found.** `files_with_ft_regions: 36`; the `#ifndef Py_GIL_DISABLED` elide in
`batched_next`/`cycle_next` was correctly read as a *fixed* T3 and not reported — that suppression is
working exactly as designed and is what let the four unfixed itertools iterators stand out.

## Repro artifacts

`/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/ft/`
— `repro_struct_unpackiter.py`, `repro_elementiter.py`, `repro_itertools.py` (grouper),
`repro_itertools2.py` (pairwise/islice), `repro_array.py`, `tsan_count_repr.py`,
`tsan_dequeiter.py`, plus captured `*.out` transcripts.
